@echo off
title 连接 Google 账号 - 天禧知识库同步
setlocal
cd /d "%~dp0"

echo ==================================================
echo    天禧知识库 到 Google 网盘  第 1 步：连接账号
echo ==================================================
echo.

echo [1/3] 检查网络...
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $t = $c.BeginConnect('www.google.com', 443, $null, $null); if ($t.AsyncWaitHandle.WaitOne(4000)) { $c.EndConnect($t); $c.Close(); exit 0 } else { $c.Close(); exit 1 } } catch { exit 1 }"
if errorlevel 1 goto :nonet
echo   网络正常。
echo.

echo [2/3] 检查是否已经连接过...
if not exist "%~dp0rclone.conf" goto :connect
"%~dp0rclone.exe" lsd gdrive: --config "%~dp0rclone.conf" --retries 1 --contimeout 8s >nul 2>&1
if errorlevel 1 goto :connect
echo   已经连接过，无需重复登录。
goto :ok

:connect
echo [3/3] 即将打开浏览器进行登录，请：
echo     1. 选择你的 Google 账号
echo     2. 点击「允许」，把网盘权限授予 rclone
echo.
echo   小提示：
echo   - 如果出现「Google 未验证此应用」警告页，
echo     点「高级」，再点「转至 rclone（不安全）」继续。
echo   - 如果浏览器没有自动打开，把接下来显示的
echo     http://127.0.0.1:53682/ 开头的链接复制到浏览器打开。
echo   - 权限说明：需要网盘完整读写权限才能上传备份，
echo     rclone 是知名开源工具，代码公开可查。
echo.
pause
"%~dp0rclone.exe" config create gdrive drive scope=drive --config "%~dp0rclone.conf"
echo.
echo   正在验证连接...
"%~dp0rclone.exe" lsd gdrive: --config "%~dp0rclone.conf" --retries 1 --contimeout 8s >nul 2>&1
if errorlevel 1 goto :fail
goto :ok

:nonet
echo   检测不到 Google 网络。
echo   请先打开 Hide.me VPN 并连接，然后再运行本程序。
goto :end

:ok
echo.
echo   【成功】Google 账号已连接。
echo   接下来运行「2-同步到Google网盘.bat」即可开始把知识库传上去。
goto :end

:fail
echo.
echo   【未完成】连接没有成功。可能的原因：
echo     - VPN 中途断开：重新连接 VPN 后再运行一次本程序
echo     - 浏览器里没有点击「允许」
echo     - Google 提示应用被阻止：截图发给天禧处理
goto :end

:end
echo.
pause
exit /b 0
