# 双色球开奖邮件通知脚本

这个项目会在每次执行时：

1. 获取最新一期双色球开奖结果。
2. 将开奖结果与你在配置文件里的下注号码逐一比对。
3. 把本期是否中奖的结果发到你配置的邮箱。
4. 用本地状态文件避免同一期重复发信，适合配合操作系统定时任务运行。

## 文件说明

- `ssq_notifier.py`：主脚本
- `config.toml.example`：配置模板
- `ssq_state.json`：脚本运行后自动生成的状态文件

## 运行环境

- Python 3.11 或更高版本

脚本使用了 Python 标准库，不需要额外安装第三方依赖。

## 配置方法

1. 复制 `config.toml.example` 为 `config.toml`
2. 修改邮箱 SMTP 信息
3. 修改你的下注号码

### 邮箱配置说明

- `smtp_host`：SMTP 服务器地址
- `smtp_port`：SMTP 端口，常见 SSL 端口为 `465`
- `username`：SMTP 登录用户名，通常就是邮箱地址
- `password`：SMTP 密码或授权码
- `from_addr`：发件人邮箱
- `to_addrs`：收件人邮箱列表
- `use_ssl`：是否直接使用 SSL
- `use_starttls`：是否使用 STARTTLS
- `subject_prefix`：邮件主题前缀

常见邮箱示例：

- QQ 邮箱：`smtp.qq.com` + 授权码
- 163 邮箱：`smtp.163.com` + 授权码
- Gmail：`smtp.gmail.com` + 应用专用密码

## 下注号码配置

每一注使用一个 `[[tickets]]`：

```toml
[[tickets]]
name = "自选1"
reds = [1, 6, 11, 16, 22, 30]
blue = 9
```

- `reds` 必须是 6 个 1-33 的不重复红球号码
- `blue` 必须是 1 个 1-16 的蓝球号码

## 使用方法

先做一次试运行：

```powershell
python .\ssq_notifier.py --dry-run
```

如果输出正常，再正式运行：

```powershell
python .\ssq_notifier.py
```

如果你想在同一期重复发送一次邮件：

```powershell
python .\ssq_notifier.py --force-send
```

## 定时任务示例

你可以在 Windows 任务计划程序里设置：

- 程序：`python`
- 参数：`C:\Users\keithon\Documents\New project\ssq_notifier.py --config C:\Users\keithon\Documents\New project\config.toml`
- 起始位置：`C:\Users\keithon\Documents\New project`

建议在双色球开奖时间之后运行，比如每周二、四、日晚上晚一点执行。

## 中奖规则

当前脚本按双色球单式票常规奖级判断：

- 6 红 + 1 蓝：一等奖
- 6 红 + 0 蓝：二等奖
- 5 红 + 1 蓝：三等奖
- 5 红 + 0 蓝，或 4 红 + 1 蓝：四等奖
- 4 红 + 0 蓝，或 3 红 + 1 蓝：五等奖
- 0/1/2 红 + 1 蓝：六等奖

## 注意事项

- 开奖数据默认优先调用中国福彩官方 JSON 接口，并带有网页抓取回退逻辑。
- 如果官网页面结构后续发生变化，可能需要调整解析规则。
- 邮件金额不会自动计算，邮件里展示的是命中奖级。
- 请通过 `--dry-run` 先确认抓取结果和你的号码配置都正常。
