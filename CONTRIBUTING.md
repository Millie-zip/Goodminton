# 开发与问题反馈

在项目根目录安装开发环境，步骤见 [README](README.md)。所有命令从项目根目录执行，离线检查如下：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/build_public_release.py --check
git diff --check
```

浏览器测试使用临时上下文和本地 HTML，通过拦截路由阻止访问真实网站。它们不读取系统凭据，不打开正式 profile，也不提交订单或发送通知。需先安装 Playwright Chromium；测试也兼容已安装的 Chrome/Edge。

新增回归测试应使用 `booking.example.json` 或临时配置，不能依赖 `booking.json`、已有数据库、真实账号或历史截图。临时数据库和输出写入临时目录。改变日期选择、场地解析或提交逻辑时，保留“目标日期核对、完整时段核对、未知提交结果停止”等保护。

提交前检查 [SECURITY.md](SECURITY.md)。不要把运行数据放进 Issue、提交记录或 PR。真实运行、校准、通知和定时任务安装具有外部或系统副作用，离线测试不应调用这些流程。

新增公开文件时，更新 `scripts/build_public_release.py` 中的明确文件清单及对应测试。发布前按 [发布说明](docs/发布说明.md) 生成并检查独立压缩包。

本项目暂未指定开源许可证，也未声明接受贡献的额外授权条款；贡献前需与维护者明确相应安排。
