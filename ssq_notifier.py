#!/usr/bin/env python3
"""Fetch the latest SSQ draw, compare configured tickets, and send email notifications."""

from __future__ import annotations

import argparse
import json
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


DEFAULT_CONFIG = "config.toml"
DEFAULT_STATE = "ssq_state.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


class NoRedirectHandler(HTTPRedirectHandler):
    """Handle anti-bot self redirects manually so cookies can be preserved."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class ConfigError(Exception):
    """Raised when the configuration file is invalid."""


class FetchError(Exception):
    """Raised when draw data cannot be fetched or parsed."""


@dataclass(frozen=True)
class Ticket:
    name: str
    reds: tuple[int, ...]
    blue: int


@dataclass(frozen=True)
class DrawResult:
    issue: str
    draw_date: str | None
    reds: tuple[int, ...]
    blue: int
    source_url: str


@dataclass(frozen=True)
class TicketOutcome:
    ticket: Ticket
    red_hits: int
    blue_hit: bool
    prize_level: str | None
    prize_name: str


def load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        raise ConfigError("当前 Python 版本不支持 tomllib，请使用 Python 3.11+ 运行该脚本。")
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件 TOML 格式错误: {exc}") from exc


def normalize_reds(values: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    if len(values) != 6:
        raise ConfigError("每注号码必须包含 6 个红球。")
    reds = tuple(sorted(int(v) for v in values))
    if len(set(reds)) != 6:
        raise ConfigError("红球号码不能重复。")
    if any(v < 1 or v > 33 for v in reds):
        raise ConfigError("红球号码必须在 1-33 之间。")
    return reds


def normalize_blue(value: int) -> int:
    blue = int(value)
    if blue < 1 or blue > 16:
        raise ConfigError("蓝球号码必须在 1-16 之间。")
    return blue


def load_config(path: Path) -> dict[str, Any]:
    raw = load_toml(path)
    email = raw.get("email")
    tickets_raw = raw.get("tickets")
    if not isinstance(email, dict):
        raise ConfigError("缺少 [email] 配置。")
    if not isinstance(tickets_raw, list) or not tickets_raw:
        raise ConfigError("至少需要配置一注 [[tickets]]。")

    tickets: list[Ticket] = []
    for idx, item in enumerate(tickets_raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"第 {idx} 注配置格式不正确。")
        name = str(item.get("name") or f"第{idx}注")
        reds = normalize_reds(item.get("reds", []))
        blue = normalize_blue(item.get("blue"))
        tickets.append(Ticket(name=name, reds=reds, blue=blue))

    result_source = raw.get("result_source", {})
    if result_source and not isinstance(result_source, dict):
        raise ConfigError("[result_source] 配置格式不正确。")

    state_file = raw.get("state_file") or DEFAULT_STATE
    return {
        "email": {
            "smtp_host": str(email.get("smtp_host") or "").strip(),
            "smtp_port": int(email.get("smtp_port") or 465),
            "username": str(email.get("username") or "").strip(),
            "password": str(email.get("password") or "").strip(),
            "from_addr": str(email.get("from_addr") or "").strip(),
            "to_addrs": [str(v).strip() for v in email.get("to_addrs", [])],
            "use_ssl": bool(email.get("use_ssl", True)),
            "use_starttls": bool(email.get("use_starttls", False)),
            "subject_prefix": str(email.get("subject_prefix") or "[双色球]"),
        },
        "tickets": tickets,
        "state_file": state_file,
        "result_source": {
            "api_url": str(
                result_source.get("api_url")
                or "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
            ),
            "list_url": str(result_source.get("list_url") or "https://www.cwl.gov.cn/ygkj/ssq/kjgg/"),
            "homepage_url": str(result_source.get("homepage_url") or "https://www.cwl.gov.cn/"),
            "timeout_seconds": int(result_source.get("timeout_seconds") or 15),
        },
    }


def validate_email_config(email_cfg: dict[str, Any]) -> None:
    required = ["smtp_host", "username", "password", "from_addr"]
    missing = [key for key in required if not email_cfg.get(key)]
    if missing:
        raise ConfigError(f"邮件配置缺少字段: {', '.join(missing)}")
    to_addrs = email_cfg.get("to_addrs") or []
    if not to_addrs:
        raise ConfigError("邮件配置至少需要一个收件人 to_addrs。")
    if email_cfg["use_ssl"] and email_cfg["use_starttls"]:
        raise ConfigError("use_ssl 和 use_starttls 不能同时为 true。")


def fetch_text(url: str, timeout: int, accept: str | None = None) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept or "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    opener = build_opener(NoRedirectHandler)
    cookie = ""

    for _ in range(3):
        request_headers = dict(headers)
        if cookie:
            request_headers["Cookie"] = cookie
        request = Request(url, headers=request_headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="ignore")
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                set_cookie = exc.headers.get("Set-Cookie", "").split(";", 1)[0].strip()
                location = exc.headers.get("Location", "").strip()
                if set_cookie and (not location or location == url):
                    cookie = set_cookie
                    continue
            raise FetchError(f"请求失败: {url} HTTP {exc.code}") from exc
        except URLError as exc:
            raise FetchError(f"请求失败: {url} {exc.reason}") from exc

    raise FetchError(f"请求失败: {url} 触发了重复重定向。")


def fetch_latest_draw_from_api(result_source: dict[str, Any]) -> DrawResult:
    timeout = int(result_source["timeout_seconds"])
    query = urlencode(
        {
            "name": "ssq",
            "issueCount": "1",
            "issueStart": "",
            "issueEnd": "",
            "dayStart": "",
            "dayEnd": "",
            "pageNo": "1",
            "pageSize": "1",
            "week": "",
            "systemType": "PC",
        }
    )
    api_url = f"{result_source['api_url']}?{query}"
    body = fetch_text(api_url, timeout, accept="application/json,text/plain,*/*")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError("官方开奖接口返回的不是有效 JSON。") from exc

    result = (payload.get("result") or [])
    if not result:
        raise FetchError("官方开奖接口未返回双色球开奖数据。")

    latest = result[0]
    reds_text = str(latest.get("red") or "")
    blue_text = str(latest.get("blue") or "")
    reds = tuple(sorted(int(item) for item in reds_text.split(",") if item.strip()))
    blue = int(blue_text)
    if len(reds) != 6:
        raise FetchError("官方开奖接口返回的红球数量不是 6 个。")

    draw_date = str(latest.get("date") or "").split("(", 1)[0].strip() or None
    return DrawResult(
        issue=str(latest.get("code") or ""),
        draw_date=draw_date,
        reds=reds,
        blue=blue,
        source_url=api_url,
    )


def clean_html_text(value: str) -> str:
    text = re.sub(r"<script.*?>.*?</script>", " ", value, flags=re.S | re.I)
    text = re.sub(r"<style.*?>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text)


def parse_draw_from_article(html: str, source_url: str) -> DrawResult:
    text = clean_html_text(html)
    issue_match = re.search(r"双色球[^\d]{0,20}第(\d{7})期", text)
    date_match = re.search(r"开奖日期[:：]\s*(\d{4}-\d{2}-\d{2})", text)
    block_match = re.search(r"开奖号码[:：]\s*(.*?)\s*中奖情况", text)
    if not issue_match or not block_match:
        raise FetchError("无法从开奖公告页解析期号或开奖号码。")

    number_candidates = re.findall(r"(?<!\d)(\d{1,2})(?!\d)", block_match.group(1))
    numbers = [int(item) for item in number_candidates[:7]]
    if len(numbers) < 7:
        raise FetchError("开奖公告页中的号码数量不足 7 个。")

    reds = tuple(sorted(numbers[:6]))
    blue = numbers[6]
    return DrawResult(
        issue=issue_match.group(1),
        draw_date=date_match.group(1) if date_match else None,
        reds=reds,
        blue=blue,
        source_url=source_url,
    )


def parse_draw_from_list_page(html: str, base_url: str, timeout: int) -> DrawResult:
    article_patterns = [
        r'href="(?P<href>/c/\d{4}/\d{2}/\d{2}/\d+\.shtml)"[^>]*>[^<]*双色球[^<]*第(?P<issue>\d{7})期[^<]*开奖公告',
        r'href="(?P<href>/c/\d{4}/\d{2}/\d{2}/\d+\.shtml)".{0,300}?双色球.{0,80}?第(?P<issue>\d{7})期.{0,80}?开奖公告',
    ]
    for pattern in article_patterns:
        match = re.search(pattern, html, flags=re.S | re.I)
        if not match:
            continue
        article_url = urljoin(base_url, match.group("href"))
        article_html = fetch_text(article_url, timeout)
        return parse_draw_from_article(article_html, article_url)
    raise FetchError("无法从往期开奖列表找到最新双色球开奖公告链接。")


def parse_draw_from_homepage(html: str, homepage_url: str) -> DrawResult:
    text = clean_html_text(html)
    block_match = re.search(
        r"双色球\s+第(?P<issue>\d{7})期(?P<body>.*?)每周二、四、日开奖",
        text,
        flags=re.S,
    )
    if not block_match:
        raise FetchError("无法从首页找到双色球区域。")

    numbers = [int(item) for item in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", block_match.group("body"))[:7]]
    if len(numbers) < 7:
        raise FetchError("首页中的号码数量不足 7 个。")

    return DrawResult(
        issue=block_match.group("issue"),
        draw_date=None,
        reds=tuple(sorted(numbers[:6])),
        blue=numbers[6],
        source_url=homepage_url,
    )


def fetch_latest_draw(result_source: dict[str, Any]) -> DrawResult:
    errors: list[str] = []

    try:
        return fetch_latest_draw_from_api(result_source)
    except FetchError as exc:
        errors.append(str(exc))

    timeout = int(result_source["timeout_seconds"])
    list_url = result_source["list_url"]
    try:
        list_html = fetch_text(list_url, timeout)
        return parse_draw_from_list_page(list_html, list_url, timeout)
    except FetchError as exc:
        errors.append(str(exc))

    homepage_url = result_source["homepage_url"]
    try:
        homepage_html = fetch_text(homepage_url, timeout)
        return parse_draw_from_homepage(homepage_html, homepage_url)
    except FetchError as exc:
        errors.append(str(exc))

    joined = " | ".join(errors)
    raise FetchError(f"获取最新开奖结果失败。{joined}")


def evaluate_ticket(ticket: Ticket, draw: DrawResult) -> TicketOutcome:
    red_hits = len(set(ticket.reds) & set(draw.reds))
    blue_hit = ticket.blue == draw.blue

    prize_level: str | None = None
    if red_hits == 6 and blue_hit:
        prize_level = "一等奖"
    elif red_hits == 6:
        prize_level = "二等奖"
    elif red_hits == 5 and blue_hit:
        prize_level = "三等奖"
    elif red_hits == 5 or (red_hits == 4 and blue_hit):
        prize_level = "四等奖"
    elif red_hits == 4 or (red_hits == 3 and blue_hit):
        prize_level = "五等奖"
    elif blue_hit and red_hits in {0, 1, 2}:
        prize_level = "六等奖"

    prize_name = prize_level or "未中奖"
    return TicketOutcome(
        ticket=ticket,
        red_hits=red_hits,
        blue_hit=blue_hit,
        prize_level=prize_level,
        prize_name=prize_name,
    )


def format_numbers(reds: tuple[int, ...], blue: int) -> str:
    red_text = " ".join(f"{item:02d}" for item in reds)
    return f"{red_text} + {blue:02d}"


def build_email(draw: DrawResult, outcomes: list[TicketOutcome], subject_prefix: str) -> tuple[str, str]:
    winning_count = sum(1 for item in outcomes if item.prize_level)
    subject = f"{subject_prefix} 第{draw.issue}期 {('有中奖' if winning_count else '未中奖')}"

    lines = [
        f"双色球第 {draw.issue} 期开奖结果",
        f"开奖日期: {draw.draw_date or '未解析到，建议以官网页面为准'}",
        f"开奖号码: {format_numbers(draw.reds, draw.blue)}",
        f"来源: {draw.source_url}",
        "",
        "投注结果:",
    ]
    for outcome in outcomes:
        lines.append(
            (
                f"- {outcome.ticket.name}: {format_numbers(outcome.ticket.reds, outcome.ticket.blue)} "
                f"| 红球命中 {outcome.red_hits} 个 | 蓝球命中 {'是' if outcome.blue_hit else '否'} "
                f"| 结果: {outcome.prize_name}"
            )
        )
    if winning_count == 0:
        lines.extend(["", "本期没有命中任何奖级。"])
    else:
        lines.extend(["", f"本期共有 {winning_count} 注命中奖级，请及时登录官网或前往正规渠道核对兑奖信息。"])

    return subject, "\n".join(lines)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_email(email_cfg: dict[str, Any], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg["from_addr"]
    msg["To"] = ", ".join(email_cfg["to_addrs"])
    msg.set_content(body)

    if email_cfg["use_ssl"]:
        with smtplib.SMTP_SSL(
            email_cfg["smtp_host"],
            email_cfg["smtp_port"],
            context=ssl.create_default_context(),
            timeout=30,
        ) as server:
            server.login(email_cfg["username"], email_cfg["password"])
            server.send_message(msg)
        return

    with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=30) as server:
        server.ehlo()
        if email_cfg["use_starttls"]:
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        server.login(email_cfg["username"], email_cfg["password"])
        server.send_message(msg)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="双色球开奖结果邮件通知脚本")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"配置文件路径，默认: {DEFAULT_CONFIG}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印结果，不发送邮件，也不更新状态文件。",
    )
    parser.add_argument(
        "--force-send",
        action="store_true",
        help="即使本地状态里已经通知过当前期，也强制发送一次邮件。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()

    try:
        config = load_config(config_path)
        if not args.dry_run:
            validate_email_config(config["email"])
        draw = fetch_latest_draw(config["result_source"])
        outcomes = [evaluate_ticket(ticket, draw) for ticket in config["tickets"]]
        subject, body = build_email(draw, outcomes, config["email"]["subject_prefix"])
    except (ConfigError, FetchError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    state_path = (config_path.parent / config["state_file"]).resolve()
    state = load_state(state_path)
    already_sent = state.get("last_notified_issue") == draw.issue

    if args.dry_run:
        print(subject)
        print("=" * len(subject))
        print(body)
        return 0

    if already_sent and not args.force_send:
        print(f"[INFO] 第 {draw.issue} 期已通知过，跳过发送。")
        return 0

    try:
        send_email(config["email"], subject, body)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 邮件发送失败: {exc}", file=sys.stderr)
        return 1

    save_state(
        state_path,
        {
            "last_notified_issue": draw.issue,
            "last_notified_at": datetime.now().isoformat(timespec="seconds"),
            "source_url": draw.source_url,
        },
    )
    print(f"[INFO] 已发送第 {draw.issue} 期通知邮件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
