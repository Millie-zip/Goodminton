# 北航羽毛球预约助手

这个项目在 Windows 电脑上运行，用来处理北航羽毛球场预约中可以自动完成的步骤：前一晚从 iPhone 设置是否预约和时间，第二天打开场馆 `38`、`39`，寻找符合时间的任意空场，选择同伴并提交。验证码出现后，程序通知你远程操作电脑；进入支付页后再次提醒。

当前版本已经包含配置、手机设置页、系统凭据、定时入口、场地候选逻辑和通知功能。北航预约页需要校园网和登录账号，本机暂时无法确认登录后的 HTML 结构，因此 `slot_candidates` 默认留空，`dry_run` 默认开启。在页面校准完成前，程序不会点击场地或提交订单。

程序不会自动识别图像验证码，也不会在支付超时后循环下单。

## 运行环境

- Windows 10/11
- Python 3.12
- 可以访问 `https://cgyy.buaa.edu.cn/` 的校园网或学校 VPN
- iPhone 上安装免费的 Bark，用于接收计划、验证码和支付提醒
- 电脑和 iPhone 安装 Tailscale Personal（个人使用免费），手机设置页只在自己的 tailnet 内开放
- 验证码可用免费的 Chrome Remote Desktop 或 RustDesk 手动点选

## 安装

本项目后续统一使用 Conda，不使用 `.venv`。以下命令都在 Windows PowerShell 中运行：

```powershell
conda create --solver classic --prefix "D:\Millie\conda\envs\buaa-booking" python=3.12 pip --yes
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
cd D:\Millie\羽毛球\抢票脚本
python -m pip install -e ".[test]"
python -m playwright install chromium
python -m buaa_booking init
```

以后每次打开新的 PowerShell，都先运行：

```powershell
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
cd D:\Millie\羽毛球\抢票脚本
```

这里直接调用 `conda`，绕过当前 PowerShell 中有问题的 Conda 包装函数。环境目录使用 `buaa-booking`，不含下划线，避免误输入 `\_`。

`init` 会从 `config/booking.example.json` 创建 `config/booking.json`。真实配置、浏览器状态、SQLite 数据库和运行截图都已加入 `.gitignore`。

## 配置免费的 iPhone 通知

在 iPhone 的 App Store 安装 Bark。打开 Bark，复制首页显示的测试地址。首次配置时可以粘贴完整测试地址，也可以只填 `api.day.app/` 后面的 Device Key。不要把测试地址或 Device Key 发给别人，并在 iOS 设置中允许 Bark 通知；若要在静音或专注模式下响铃，还要允许重要警告。

然后运行：

```powershell
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
python -m buaa_booking setup-secrets
```

命令会要求输入：

- 北航学号
- 统一身份认证密码
- Bark Device Key

这些内容写入 Windows 系统凭据库。配置文件和 SQLite 数据库不保存密码。

普通计划提醒只显示通知；验证码、登录异常和支付提醒会使用 Bark 的紧急通知，并按当前配置持续响铃 30 秒。点通知会打开手机设置页或远程控制入口。

先测试手机通知：

```powershell
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
python -m buaa_booking test-notification
```

如果提示“Bark Device Key 尚未在当前服务器注册”，不要再次粘贴报错中的长串字符。按下面做：

1. 在 iPhone 打开 Bark，允许通知并停留几秒；
2. 在 Bark 首页重新复制“测试地址”；
3. 重新运行 `setup-secrets`，把完整测试地址粘贴进去以覆盖旧 Key；
4. 再运行 `test-notification`。

官方服务器 `https://api.day.app` 只能配合它自己签发的测试地址；如果你在 Bark 中切换了自建服务器，必须同时把 `notification.bark_server` 改成相同服务器地址。

## 手机设置页：从哪里打开、每天怎么用

这不是北航场馆网页，而是本程序自己的“明早预约开关”。它只让你在手机上决定：**明天早上要不要运行、抢几点到几点**。它不会让你在手机上选具体球场，也不会替你支付。

### 第一次配置：在 Windows 电脑操作

先安装 Tailscale。它不属于 Python 项目依赖，激活 Conda 环境也不会自动安装它。

**Windows 电脑：**

1. 用浏览器打开 [Tailscale Windows 官方下载页](https://tailscale.com/download/windows)；
2. 下载并双击运行 Windows 安装程序；出现 Windows 权限提示时，点“是”；
3. 安装结束后，在 Windows 任务栏右下角点击 `^`，找到 Tailscale 图标；右键图标，选择“Log in / 登录”，在浏览器完成登录；
4. 关闭当前所有 PowerShell 窗口，再重新打开一个 PowerShell；
5. 运行下面命令。能显示版本号才说明安装完成：

   ```powershell
   tailscale version
   ```

**iPhone：**

1. 打开 App Store，搜索并安装 **Tailscale**；
2. 打开 Tailscale，按提示登录，使用**与 Windows 电脑相同的账号**；
3. 回到 Tailscale 首页，确认状态显示为已连接。

Windows 和 iPhone 都登录完成后，再在 Windows 电脑的 PowerShell 中进入本项目目录：

```powershell
cd D:\Millie\羽毛球\抢票脚本
```

在第一个 PowerShell 窗口运行下面的命令。窗口要保持打开；看到“手机设置页正在监听”就表示设置页已经启动：

```powershell
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
python -m buaa_booking serve
```

再新开一个 PowerShell 窗口，仍在同一目录执行：

```powershell
tailscale serve --bg 8765
```

第一次运行时，Tailscale 可能会打开网页要求确认 HTTPS；按网页提示允许即可。命令会显示一个类似下面的地址：

```text
https://你的电脑名.你的-tailnet.ts.net
```

复制这个地址，打开 `config/booking.json`，找到 `dashboard`，把它填进 `public_url`：

```json
"dashboard": {
  "host": "127.0.0.1",
  "port": 8765,
  "public_url": "https://你的电脑名.你的-tailnet.ts.net"
}
```

保存文件。以后想确认地址是否仍有效，可在电脑运行：

```powershell
tailscale serve status
```

`serve --bg` 会让 Tailscale 持续转发；设置页本身仍只在电脑本机监听。电脑重启后，要先让 Windows 登录并启动本程序的 Dashboard 定时任务，设置页才会重新出现。

### 第一次打开：在 iPhone 操作

1. 打开 iPhone 的 **Tailscale**，确认状态为已连接；
2. 在电脑 PowerShell 执行下面的测试命令：

   ```powershell
   (conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
   python -m buaa_booking prompt
   ```

3. iPhone 会收到一条来自 **Bark** 的“请确认明早是否预约”通知；
4. 点这条通知，Safari 会打开“明早预约设置”页面；
5. 页面上的开关、开始时间、结束时间分别填写：
   - 打开“明早执行预约”：允许第二天早晨脚本执行；
   - 关闭开关：第二天早晨脚本不抢场地；
   - 开始时间 / 结束时间：例如 `19:00` 和 `21:00`；
6. 点页面最下方的“保存并确认”。看到“设置已保存”才算完成。

这个页面的链接含有一次固定访问令牌。只从 Bark 通知打开，不要转发给其他人。

### 每天晚上：iPhone 上只做这四步

在电脑已经开机、联网，并且 Dashboard 已启动的前提下：

1. 晚上 22:30，Bark 会收到“请确认明早是否预约”；
2. 点通知，打开 Safari 设置页；
3. 选择是否预约，填写想抢的时间；
4. 点“保存并确认”。

没有保存确认时，默认不会运行预约。22:50 若还未确认，Bark 会再提醒一次。

### 第二天早晨：电脑自动做什么

先在 Windows 电脑执行一次下面的命令，注册定时任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_tasks.ps1
```

运行这条命令前，PowerShell 提示符左侧应显示 `(buaa-booking)`。安装脚本会记录当前 Conda 环境中 `python.exe` 的绝对路径；第二天执行计划任务时不需要重新打开或激活 Conda。

之后不需要在 iPhone 上启动脚本。只要电脑在 06:59 前已经开机、没有休眠、能访问校园网或学校 VPN，Windows 会在 06:59 启动浏览器并预载页面，07:00 开始读取场地。

如果出现验证码或进入支付页，Bark 会响铃。此时再用 iPhone 的 Chrome Remote Desktop 或 RustDesk 连到电脑，手动完成验证码或付款。

目前 `dry_run` 仍是 `true`：即使你点击“保存并确认”，程序也只检测场地，不会提交预约。完成页面校准并确认无误后，才把它改为 `false`。

## 手机远程点验证码

验证码不能由程序自动识别。建议二选一：

- Chrome Remote Desktop：电脑开启远程访问，iPhone 安装客户端；默认配置点击 Bark 通知后打开它的访问页。
- RustDesk：电脑和 iPhone 都安装客户端，设置无人值守密码；收到 Bark 通知后手动打开 RustDesk 连接电脑。

两种方式都可免费使用。若采用 RustDesk，可把 `config/booking.json` 中的 `notification.remote_control_url` 改成你希望通知点击后打开的入口；如果 iOS 不能直接拉起 RustDesk，收到提醒后手动打开即可。

## 页面校准

校准必须在能访问北航场馆系统的网络里进行：

```powershell
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
python -m buaa_booking calibrate
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
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
python -m buaa_booking prompt
```

查看计划：

```powershell
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
python -m buaa_booking show
```

需要立即验证页面读取时，指定计划的运行日期并跳过 07:00 等待：

```powershell
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
python -m buaa_booking run --run-date 2026-09-11 --now
```

试运行只读取候选场地并保存截图，不点击场地。确认日期、时间和候选场地都正确后，才能把 `dry_run` 改成 `false`。

## 安装 Windows 定时任务

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_tasks.ps1
```

运行前确认当前 PowerShell 已激活 `buaa-booking` 环境。

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
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
python -m pytest
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
