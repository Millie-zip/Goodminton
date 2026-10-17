# 北航羽毛球预约助手

用于北航羽毛球场预约流程自动化的 Windows/Python 项目。程序可以读取预约计划，登录预约网站，选择日期和场地，选择同伴，勾选预约须知并提交订单；遇到 clickword 验证码时支持人工处理或本地 OCR 自动点击。

预约时间支持整点起止的一小时或连续两小时。填写 `16:00–18:00` 时，程序选择 `16:00–17:00` 和 `17:00–18:00` 两个格子，一次提交。优先选择同一块场地连续两小时；没有这种组合时，在同一场馆内换场补足两个时段，不跨场馆拼单。任一时段未选中时不会提交部分订单。

进入支付页后，程序会点击付款入口、生成支付二维码并发送到手机；不会点击最终支付确认按钮。

> 重要：真实流程可能创建真实订单。首次使用请先完成校准，并确认配置中的日期、时间、场馆和账号无误。

## 功能

- 本地手机设置页，用于确认是否运行第二天的预约计划
- Playwright 持久化浏览器登录状态
- 打开多个场馆页面，并按优先级尝试候选场地
- 日期选择、场地矩阵识别和配置选择器兜底
- 同伴选择、预约须知勾选和提交按钮状态检查
- `captcha/get` clickword 验证码人工/本地 OCR 处理
- Bark 或 Console 通知
- SQLite 运行状态和事件记录
- Windows 定时任务
- 页面校准、截图和本地调试产物

详细逻辑见 [docs/项目逻辑说明.md](docs/项目逻辑说明.md)。

## 运行环境

- Windows 10 或 Windows 11
- Python 3.12
- 可访问北航预约网站的网络环境（校园网或学校 VPN）
- Chromium 浏览器（由 Playwright 安装）
- 可选：Bark、Tailscale、远程桌面工具

## 安装

将项目下载或解压后，在项目根目录打开 PowerShell，执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup.ps1
.\.venv\Scripts\python.exe -m buaa_booking init
```

安装脚本会：

1. 创建项目内的 `.venv` 虚拟环境；
2. 安装当前项目及其依赖；
3. 安装 Playwright Chromium。

如果系统没有可用的 `python` 命令，请先安装 Python 3.12，并重新打开 PowerShell。

也可以手动安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m buaa_booking init
```

后续命令均可使用项目环境中的 Python：

```powershell
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $Python -m buaa_booking show
```

## 初始化配置

`init` 会根据 `config/booking.example.json` 创建本机配置：

```text
config/booking.json
```

本机配置不应提交到版本库。首次使用前检查：

- `home_url`
- `venue_urls`
- `venue_priority`
- `days_ahead`
- `default_start_time`
- `default_end_time`
- `browser.captcha_mode`
- `selectors`

配置中的路径应使用相对路径，例如：

```json
{
  "browser": {
    "profile_dir": "data/browser-profile"
  }
}
```

## 保存账号和通知凭据

账号、密码和 Bark Device Key 不写入 JSON 配置，而是保存到系统凭据管理器：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking setup-secrets
```

命令会交互式询问：

- 北航统一身份认证账号
- 统一身份认证密码
- Bark Device Key（可选）

测试通知：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking test-notification
```

不要把账号、密码、Device Key、浏览器 profile、SQLite 数据库或验证码调试图片提交到版本库。

## 页面校准

首次运行或预约页面结构发生变化后，先执行：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking calibrate
```

校准流程会：

1. 打开可见 Chromium；
2. 等待用户完成登录；
3. 打开配置中的场馆页面；
4. 采集页面截图和元素信息；
5. 将结果写入 `artifacts/`。

校准完成后，根据现场页面检查 `config/booking.json` 中的选择器：

- `date_buttons`
- `slot_candidates`
- `companion_open`
- `companion_options`
- `agreement_checkbox`
- `submit_button`

程序会优先解析场地时间矩阵，`slot_candidates` 用作选择器兜底。

## 手机设置页

启动本地设置页：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking serve
```

默认监听：

```text
http://127.0.0.1:8765
```

如需通过手机访问：

1. 在电脑和手机上登录同一个 Tailscale 网络；
2. 在另一个 PowerShell 窗口执行：

```powershell
tailscale serve --bg 8765
tailscale serve status
```

3. 将 Tailscale 显示的 HTTPS 地址写入 `config/booking.json` 的 `dashboard.public_url`；
4. 使用 Bark 通知或设置页链接打开手机设置页。

设置页只负责确认是否运行、开始时间和结束时间，不负责选择具体场地，也不负责付款。

## 正式运行

立即执行当天已保存的计划（目标预约日期由 `days_ahead` 决定）：

```powershell
$RunDate = (Get-Date).ToString("yyyy-MM-dd")
.\.venv\Scripts\python.exe -m buaa_booking run --run-date $RunDate --now
```

查看当前计划：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking show
```

发送计划确认提醒：

```powershell
.\.venv\Scripts\python.exe -m buaa_booking prompt
```

正式运行链路：

```text
登录
  -> 打开场馆页面
  -> 选择目标日期
  -> 读取可用场地
  -> 选择场地
  -> 选择同伴
  -> 勾选预约须知
  -> 等待可点击的提交按钮
  -> 提交订单
  -> 处理验证码
  -> 进入支付页并通知
```

运行状态和事件写入 `data/`，截图和校准结果写入 `artifacts/`。

## 验证码

`browser.captcha_mode` 支持：

- `manual`：检测到验证码后通知用户，由用户远程操作浏览器；
- `auto`：监听 `captcha/get` 的 `application/json` 响应，读取 `originalImageBase64` 和目标字符，使用本地 RapidOCR 点击验证码图片。

自动验证码不会调用第三方验证码识别服务。程序按位置和字符过滤图片右下角的“场馆预约”水印，不再按 OCR 结果顺序截断。自动识别仍可能失败，届时回退人工处理。

## 手机端支付

检测到支付页后，程序会：

1. 点击配置中的付款入口；
2. 等待支付页面的 `img.payImage` 图片加载；
3. 转换为 PNG 并检查二维码结构，确认后保存为 `artifacts/<run-date>/payment-qr.png`；
4. 通过带 Dashboard token 的 Bark 链接发送到手机。

手机打开通知中的链接即可查看二维码并人工完成支付。程序不会点击最终支付确认按钮，会在支付页停留并轮询 `selectors.payment_success_markers`、`selectors.payment_failure_markers`。支付成功后结束；支付失败或 60 秒超时后，程序会重新执行选择日期、场地、同伴、须知和提交订单的完整流程。付款入口可在 `selectors.payment_button` 中校准；二维码定位固定使用目标站的 `img.payImage`，防止误选验证码。

## 版本范围

项目保留运行代码、安装脚本、配置模板和必要说明。临时测试脚本、调试配置、历史实测报告及旧截图已清理；后续运行生成的数据和截图仍不纳入版本库。自动验证码可能回退人工处理，当前版本不保证 5 秒内点击支付。

## Windows 定时任务

确认已经完成安装、初始化和凭据配置后，在项目根目录执行：

```powershell
.\scripts\install_tasks.ps1
```

脚本会使用当前 Conda 环境，或使用参数显式指定 Python：

```powershell
.\scripts\install_tasks.ps1 -PythonExe ((Resolve-Path .\.venv\Scripts\python.exe).Path)
```

默认注册以下任务：

| 任务 | 默认时间 | 作用 |
|---|---:|---|
| `BUAA-Badminton-Dashboard` | 登录、22:29、06:58 | 启动手机设置页 |
| `BUAA-Badminton-Prompt` | 22:30 | 发送计划确认提醒 |
| `BUAA-Badminton-Reminder` | 22:45–01:45，每 15 分钟 | 提醒未确认计划；收到手机反馈后停用 |
| `BUAA-Badminton-Run` | 06:59 | 启动预约流程 |

下一次晚间计划提醒会按计划确认状态重新启用或停用提醒任务。具体时间以配置和脚本当前实现为准。

## 常用命令

```powershell
# 初始化本机配置
.\.venv\Scripts\python.exe -m buaa_booking init

# 保存账号和通知凭据
.\.venv\Scripts\python.exe -m buaa_booking setup-secrets

# 启动手机设置页
.\.venv\Scripts\python.exe -m buaa_booking serve

# 页面校准
.\.venv\Scripts\python.exe -m buaa_booking calibrate

# 查看计划
.\.venv\Scripts\python.exe -m buaa_booking show

# 执行当天已保存的预约计划
$RunDate = (Get-Date).ToString("yyyy-MM-dd")
.\.venv\Scripts\python.exe -m buaa_booking run --run-date $RunDate --now
```

## 目录结构

```text
README.md                    安装、配置和日常使用
pyproject.toml               包定义、依赖和命令入口
requirements.txt             pip 依赖清单
config/
  booking.example.json       可提交的配置模板
  booking.json               本机配置，不提交
scripts/
  setup.ps1                  安装项目和 Playwright
  install_tasks.ps1          注册 Windows 定时任务
src/buaa_booking/            主程序代码
data/
  browser-profile/           正式浏览器登录状态，不提交
  state.sqlite3              正式预约计划和运行事件，不提交
artifacts/                   运行时生成的截图、支付二维码和校准结果
docs/
  项目逻辑说明.md            逻辑板块说明
```

`src/` 按预约流程、验证码、手机设置页、配置、状态存储和通知划分模块。安装脚本和定时任务通过 `python -m buaa_booking` 启动，运行路径保持固定。

清理旧文件时，可删除已经不需要的 `artifacts/` 历史输出和 Python 缓存。保留 `config/booking.json`、`data/state.sqlite3` 和 `data/browser-profile/`，以保存正式配置、计划及登录状态。`src/*.egg-info/` 是本机可编辑安装生成的包元数据，不作为测试产物清理。

## 安全和运行边界

- 只在有权访问的账号和预约系统中使用。
- `run` 会创建真实订单，运行前应确认配置和已保存的计划。
- 程序只点击付款入口，不会点击最终支付确认按钮。
- 不要共享手机设置页 URL、Dashboard token、账号凭据或验证码图片。
- 不要把 `config/booking.json`、`data/`、`artifacts/` 或浏览器 profile 提交到版本库。
- 页面 DOM、接口字段和验证码图片版式变化后，应重新校准并检查日志。

## 开发检查

修改代码后可执行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

真实流程测试必须连接目标网站，不能在离线环境中假定其结果。
