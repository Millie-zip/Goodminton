# 北航羽毛球预约助手

这个项目在 Windows 电脑上运行，用来处理北航羽毛球场预约中可以自动完成的步骤：前一晚从 iPhone 设置是否预约和时间，第二天打开场馆 `38`、`39`，寻找符合时间的任意空场，选择同伴并提交。验证码出现后，程序通知你远程操作电脑；进入支付页后再次提醒。

当前版本已经包含配置、手机设置页、系统凭据、定时入口、场地候选逻辑和通知功能。北航预约页需要校园网和登录账号，本机暂时无法确认登录后的 HTML 结构，因此 `slot_candidates` 默认留空，`dry_run` 默认开启。在页面校准完成前，程序不会点击场地或提交订单。

程序不会自动识别图像验证码，也不会在支付超时后循环下单。

## 运行环境

- Windows 10/11
- Python 3.12
- 可以访问 `https://cgyy.buaa.edu.cn/` 的校园网或学校 VPN
- iPhone 上安装 Pushover 和远程桌面客户端
- 建议在电脑和 iPhone 上安装 Tailscale，手机设置页只在自己的 tailnet 内开放

## 安装

在项目目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
.\.venv\Scripts\python.exe -m buaa_booking init
```

`init` 会从 `config/booking.example.json` 创建 `config/booking.json`。真实配置、浏览器状态、SQLite 数据库和运行截图都已加入 `.gitignore`。

## 保存账号和通知密钥

```powershell
.\.venv\Scripts\python.exe -m buaa_booking setup-secrets
```

命令会要求输入：

- 北航学号
- 统一身份认证密码
- Pushover User Key
- Pushover App Token

这些内容写入 Windows 系统凭据库。配置文件和 SQLite 数据库不保存密码。

先测试手机通知：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking test-notification
```

## 手机设置页

设置页只监听 `127.0.0.1:8765`，不能直接从公网访问。安装并登录 Tailscale 后，可以用 Tailscale Serve 转发：

```powershell
tailscale serve --bg 8765
```

把命令返回的 HTTPS 地址填入 `config/booking.json`：

```json
"dashboard": {
  "host": "127.0.0.1",
  "port": 8765,
  "public_url": "https://电脑名.你的-tailnet.ts.net"
}
```

手动启动设置页：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking serve
```

访问链接还需要一个随机令牌。令牌保存在 Windows 系统凭据库，提醒通知会把完整链接发到你的 iPhone。HTTP 访问日志被关闭，防止令牌写进日志。

## 页面校准

校准必须在能访问北航场馆系统的网络里进行：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking calibrate
```

程序会打开可见的 Chromium。按终端提示完成登录、进入两个场馆的时间选择页面，不要提交订单。校准结果写入 `artifacts/calibration-时间/`：

- `venue-38.png`
- `venue-39.png`
- `element-inventory.json`

截图可能含姓名或预约信息，只用于本地校准，不要公开上传。元素清单会隐藏与时间、预约按钮无关的文字。

根据校准结果填写 `config/booking.json` 中的 `selectors.slot_candidates`。示例：

```json
"slot_candidates": [
  "button.time-slot",
  "[data-time='{start}-{end}']"
]
```

这里的示例不代表北航页面的真实选择器。必须以校准结果为准。

## 试运行

保持：

```json
"dry_run": true
```

先发送当晚设置提醒，在手机上保存明早计划：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking prompt
```

查看计划：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking show
```

需要立即验证页面读取时，指定计划的运行日期并跳过 07:00 等待：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking run --run-date 2026-09-11 --now
```

试运行只读取候选场地并保存截图，不点击场地。确认日期、时间和候选场地都正确后，才能把 `dry_run` 改成 `false`。

## 安装 Windows 定时任务

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_tasks.ps1
```

脚本为当前 Windows 用户注册四个任务：

| 任务 | 时间 | 用途 |
|---|---:|---|
| `BUAA-Badminton-Dashboard` | 登录后 | 启动手机设置页 |
| `BUAA-Badminton-Prompt` | 22:30 | 询问明早是否预约 |
| `BUAA-Badminton-Reminder` | 22:50 | 未确认时再次提醒 |
| `BUAA-Badminton-Run` | 06:59 | 登录、预载页面并等待 07:00 |

没有在手机设置页确认时，计划保持关闭，早晨任务直接结束。

## 预约状态

- `draft`：尚未从手机确认
- `confirmed`：已经保存明早计划
- `running`：正在登录或读取场地
- `captcha`：等待你远程点选验证码
- `payment`：已经进入支付页
- `dry_run`：试运行结束，没有点击
- `skipped`：当天没有启用预约
- `failed`：页面结构、网络或登录状态不符合预期，程序停止

提交后的页面状态无法确认时，程序不会继续尝试其他场地，避免重复订单。失败截图保存在 `artifacts/运行日期/`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

单元测试不访问北航网站，也不会发送真实预约请求。

## 目录

```text
config/                示例配置和本地配置
scripts/               安装依赖、注册定时任务
src/buaa_booking/      程序代码
tests/                 不访问真实网站的单元测试
data/                  浏览器登录状态和 SQLite 数据库，不进版本控制
artifacts/             校准与失败截图，不进版本控制
```

