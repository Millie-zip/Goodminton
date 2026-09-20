# 北航羽毛球预约助手

这个项目在 Windows 电脑上运行，用来处理北航羽毛球场预约中可以自动完成的步骤：前一晚从 iPhone 设置是否预约和时间，第二天打开场馆 `38`、`39`，寻找符合时间的任意空场，选择同伴并提交。验证码出现后，程序通知你远程操作电脑；进入支付页后再次提醒。

当前版本已经包含配置、手机设置页、系统凭据、定时入口、场地候选逻辑和通知功能。程序只按正式预约流程运行，不再提供试运行分支。北航预约页需要校园网和登录账号，本机暂时无法确认登录后的 HTML 结构，因此 `slot_candidates` 默认留空；完成页面校准前，程序会报告配置错误，不会点击场地。

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

计划提醒照常显示。预约启动、跳过、登录异常和运行错误使用 Bark 静默通知，不响铃；只有进入人工验证码或支付页面时才使用紧急通知，并按当前配置持续响铃 30 秒。点通知会打开手机设置页或远程控制入口。

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

`serve --bg` 会让 Tailscale 持续转发；设置页本身仍只在电脑本机监听。安装 Windows 定时任务后，Dashboard 会立即启动，并在登录 Windows、每天 22:29 和 06:58 时自动检查启动。

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

这个页面的链接含有私密访问令牌和本次计划的运行日期。只从 Bark 通知打开，不要转发给其他人。即使你在零点以后才打开通知，页面也会保存到通知指定的那次早晨任务，不会错误地顺延一天；过期链接会直接提示重新使用最新通知。

### 每天晚上：iPhone 上只做这四步

在电脑已经开机、联网的前提下，Dashboard 会在通知发出前自动启动：

1. 晚上 22:30，Bark 会收到“请确认明早是否预约”；
2. 点通知，打开 Safari 设置页；
3. 选择是否预约，填写想抢的时间；
4. 点“保存并确认”。

没有保存确认时，默认不会运行预约。22:50 若还未确认，Bark 会再提醒一次。

程序发送设置通知前会先检查电脑上的设置页。检查通过后，Bark 通知才会带可点击链接；如果设置页没有启动，手机收到的是“手机设置页未启动”，不会再打开无效页面。

### 第二天早晨：电脑自动做什么

先在 Windows 电脑执行一次下面的命令，注册定时任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_tasks.ps1
```

运行这条命令前，PowerShell 提示符左侧应显示 `(buaa-booking)`。安装脚本会记录当前 Conda 环境中 `python.exe` 的绝对路径，并立即启动 Dashboard；第二天执行计划任务时不需要重新打开或激活 Conda。

之后不需要在 iPhone 上启动脚本。只要电脑在 06:59 前已经开机、没有休眠、能访问校园网或学校 VPN，Windows 会在 06:59 启动浏览器并预载页面，07:00 开始读取场地。

早晨任务每次运行都会发一条静默状态通知：计划未启用会提示“今日预约未执行”；已启用会先提示“预约程序已启动”；配置未完成、没有空场或发生错误时，会说明最后执行到的步骤和停止原因。详细步骤同时写入本地事件日志，避免发送通知拖慢抢场。

正式提交后，程序会等待最多 15 秒识别页面变化，并同时检查主页面和嵌入页面。自动模式会监听 `captcha/get` 的 JSON 响应，读取 `data.originalImageBase64`（兼容顶层字段），用本地 OCR 识别 clickWord 图片并点击页面中的验证码图片；无法确认或求解失败时回退人工处理。进入支付页时程序停止，不会自动付款。若 15 秒后仍无法判断，程序会停止，避免重复提交，并在当天的 `artifacts` 目录保存提交前、提交后或状态未知时的截图。

程序目前只保留正式预约路径。只要计划已启用且选择器配置完整，就会选择场地、同伴、勾选须知并提交。

## 手机远程点验证码

`config/booking.json` 中的 `browser.captcha_mode` 支持两个值：

- `manual`：默认模式。检测到验证码后紧急提醒，由你远程操作电脑完成。
- `auto`：监听 `captcha/get` 的 `application/json` 响应，提取 `originalImageBase64`，由本地 OCR 识别并点击 clickWord 验证码；接口数据不完整、图片无法识别或结果无法确认时自动回退到 `manual`。

当前实际配置仍为 `manual`。人工处理可选择下面任一方式：

- Chrome Remote Desktop：电脑开启远程访问，iPhone 安装客户端；默认配置点击 Bark 通知后打开它的访问页。
- RustDesk：电脑和 iPhone 都安装客户端，设置无人值守密码；收到 Bark 通知后手动打开 RustDesk 连接电脑。

两种方式都可免费使用。若采用 RustDesk，可把 `config/booking.json` 中的 `notification.remote_control_url` 改成你希望通知点击后打开的入口；如果 iOS 不能直接拉起 RustDesk，收到提醒后手动打开即可。

自动接口定义在 `src/buaa_booking/captcha.py`。`LocalClickCaptchaSolver` 接收提交后捕获的验证码响应和解码后的图片，并在确认点击流程完成后返回 `True`；异常会转换为 `AutoCaptchaUnavailable`，预约流程随后回退到人工处理。

本地测试求解器默认 `save_debug_artifacts=False`，验证码图片、OCR 结果和失败截图只保留在内存中，不写入磁盘。显式开启调试保留后，每次处理会创建独立的 `captcha-attempt-*` 子目录，文件权限会尽量限制为当前用户可读写，默认保留 3600 秒，并在下一次创建调试目录时清理过期内容。不要把包含验证码调试文件的 `artifacts` 目录同步或共享到外部服务。

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

## 正式运行前检查

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

`run` 命令现在会执行正式预约。当前 `selectors.slot_candidates` 仍为空时，程序只会静默报告“预约配置未完成”；必须先完成页面校准并填写真实选择器。

## 安装 Windows 定时任务

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_tasks.ps1
```

运行前确认当前 PowerShell 已激活 `buaa-booking` 环境。

脚本为当前 Windows 用户注册四个任务：

| 任务 | 时间 | 用途 |
|---|---:|---|
| `BUAA-Badminton-Dashboard` | 安装后立即启动；登录后、22:29、06:58 自动补启动 | 保证手机设置页在通知发出前可访问 |
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
- `skipped`：当天没有启用预约
- `failed`：页面结构、网络或登录状态不符合预期，程序停止

提交后的页面状态无法确认时，程序不会继续尝试其他场地，避免重复订单。失败截图保存在 `artifacts/运行日期/`。

## 测试

连接校园网或学校 VPN 后，先执行不提交订单的页面校准：

```powershell
(conda shell.powershell activate "D:\Millie\conda\envs\buaa-booking") | Out-String | Invoke-Expression
python tests/test_full_flow.py --calibrate
```

需要真实提交测试时运行：

```powershell
python tests/test_full_flow.py --allow-submit --start-time 20:00 --end-time 21:00
```

测试会连接北航真实网页，预约运行日后第 2 天，并使用单独的 `data/live-test-state.sqlite3`，不会改写正式定时任务的计划。程序会自动提交统一身份认证并验证登录状态，然后打开两个场馆、选择日期和空场、选择第一位可用同伴并勾选须知。点击提交前，必须在控制台输入“确认提交”；提交后由本地 OCR 自动识别并点击验证码，失败时停止，不回退人工处理。检测到支付页后程序不会付款，检查完页面后按 Enter 关闭浏览器。

## 目录

```text
config/                示例配置和本地配置
scripts/               安装依赖、注册定时任务
src/buaa_booking/      程序代码
tests/                 不访问真实网站的单元测试
data/                  浏览器登录状态和 SQLite 数据库，不进版本控制
artifacts/             校准与失败截图，不进版本控制
```
