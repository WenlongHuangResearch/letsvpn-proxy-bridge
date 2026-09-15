# TRICOS Proxy Launcher

这是一个只监听 `127.0.0.1` 的本地 HTTP 代理，支持普通 HTTP 请求和 HTTPS `CONNECT`。它使用 Windows 当前网络作为出口，不控制 LetsVPN。

## 使用

仓库已包含构建好的 `dist\ProxyLauncher.exe`，克隆后可直接运行，无需安装 Python 或自行构建。

1. 打开 LetsVPN / 快连并确认已经连接。
2. 双击 `dist\ProxyLauncher.exe`，保持窗口运行。
3. 使用 VS Code SSH 连接 Linux。Linux 的 `http_proxy` / `https_proxy` 通过既有 SSH RemoteForward 访问 Windows 代理。

默认监听 `127.0.0.1:10808`。双击 EXE 会打开窗口，显示代理状态、监听地址和联网结果：

- 点击标题栏的「—」或 **Minimize**，窗口最小化到 Windows 任务栏，代理继续运行。点击任务栏图标可恢复窗口。
- 点击 **Test Connection**，重新检查通过本地代理访问 Google 是否成功。
- 点击 **Stop Proxy** 或右上角「×」，停止本次启动的代理并退出。
- 端口已被本程序使用时，窗口保留并显示 `Proxy is already running`；被其他程序占用时显示 PID 和进程名称。关闭提示窗口不会影响占用端口的程序。

这里的最小化是 Windows 任务栏窗口按钮，不是右下角通知区域的托盘图标。

开发或测试时可以使用临时端口，例如：

```powershell
python src\main.py --console --port 18080
curl.exe -x http://127.0.0.1:18080 -I https://www.google.com
```

端口也可以用 `TRICOS_PROXY_PORT` 设置。只允许监听 `127.0.0.1`。程序启动时会单独报告代理启动状态和 Internet 测试状态；联网失败时代理继续运行，并提示检查 LetsVPN。联网检查在程序内完成，不依赖外部 curl.exe。

EXE 日志位于 EXE 同目录的 `logs\proxy.log`（本项目中为 `dist\logs\proxy.log`）。目录不可写时改存 `%LOCALAPPDATA%\TRICOS Proxy\logs\proxy.log`。从源码运行时日志在项目的 `logs\proxy.log`。单个文件最多 5 MB，保留 3 个备份。

如果端口已被别的进程占用，GUI 会保留窗口并显示占用 PID/进程名，不会终止、抢占或修改该进程。

## 构建单文件 EXE

在已安装 Python 3.10+ 的 Windows PowerShell 中执行：

```powershell
.\build.bat
```

生成文件：`dist\ProxyLauncher.exe`（Windows x64，单文件 GUI，已提交到仓库）。最终用户只需要这个 EXE，不需要安装 Python、pip 或 proxy.py。构建会创建项目独立的 `.venv`，并打包 Python 和 Tk。构建失败会返回错误，不会误报构建成功。

## 验收

开发阶段使用 `18080`，避免影响机器上已有的 `10808`：

```powershell
.\dist\ProxyLauncher.exe --port 18080
netstat -ano | findstr :18080
curl.exe --noproxy "" -x http://127.0.0.1:18080 -I https://www.google.com
```

窗口应显示 `Running`、`Connected`。最小化后再次执行 curl；恢复窗口并关闭，再检查 `18080` 不再有 `LISTENING`。重复打开使用相同临时端口的 EXE，应显示占用提示，原实例继续可用。

运行本地回归测试（使用操作系统分配的临时端口，不使用 `10808`）：

```powershell
python -m unittest discover -s tests -v
```

从源码运行 Console 模式可用 `python src\main.py --console --port 18080`，按 Ctrl+C 退出。

只有在确认现有 `proxy.py` 已由用户手动关闭后，才用最终 EXE 测试 `10808`。程序不会停止 VPN、SSH、VS Code Remote SSH 或其他网络进程。
