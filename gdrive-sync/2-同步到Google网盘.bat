@echo off
title 同步到 Google 网盘 - 天禧知识库
setlocal
cd /d "%~dp0"
set QUIET=%~1

echo ==================================================
echo    天禧知识库 到 Google 网盘  同步
echo    %date% %time%
echo ==================================================
echo.

if not exist "%~dp0rclone.exe" goto :noexe
if not exist "%~dp0rclone.conf" goto :noconf

echo   检查网络...
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $t = $c.BeginConnect('www.google.com', 443, $null, $null); if ($t.AsyncWaitHandle.WaitOne(4000)) { $c.EndConnect($t); $c.Close(); exit 0 } else { $c.Close(); exit 1 } } catch { exit 1 }"
if errorlevel 1 goto :nonet

set RC=%~dp0rclone.exe
set CONF=%~dp0rclone.conf
set SRC=C:\ProgramData\Lenovo\AIAgent\kd\user\10338710475
set OVL=C:\Users\intpj\Tianxi\kb-server\data\overlay
set DEST=gdrive:知识库备份

echo.
echo   [1/5] 笔记内容 cusnote
"%RC%" copy "%SRC%\cusnote" "%DEST%/cusnote" --config "%CONF%" --transfers 8 --checkers 16 --progress --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
echo.
echo   [2/5] 缩略图 cloudthumb
"%RC%" copy "%SRC%\cloudthumb" "%DEST%/cloudthumb" --config "%CONF%" --transfers 8 --checkers 16 --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
echo.
echo   [3/5] 缩略图 notethumb
"%RC%" copy "%SRC%\notethumb" "%DEST%/notethumb" --config "%CONF%" --transfers 8 --checkers 16 --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
echo.
echo   [4/5] 索引数据库 db
"%RC%" copy "%SRC%\db" "%DEST%/db" --config "%CONF%" --transfers 8 --checkers 16 --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
echo.
echo   [5/5] 手机端笔记
"%RC%" copy "%OVL%" "%DEST%/手机端笔记" --config "%CONF%" --transfers 8 --checkers 16 --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
goto :done

:noexe
echo   [错误] 找不到 rclone.exe，请确认本程序在 gdrive-sync 文件夹里运行。
goto :end

:noconf
echo   [提示] 还没有连接 Google 账号。
echo   请先运行「1-连接Google账号.bat」完成登录。
goto :end

:nonet
echo   [提示] 检测不到 Google 网络，VPN 可能没有连接。
echo   请先连接 Hide.me VPN 后再同步。
goto :end

:done
echo.
echo   同步结束。打开 Google 网盘查看「知识库备份」文件夹。
echo   如果上面出现 ERROR 字样，请把本窗口截图发给天禧。

:end
echo.
if /i not "%QUIET%"=="q" pause
exit /b 0
