@echo off
title ͬ Google  - ֪ʶ
setlocal
cd /d "%~dp0"
set QUIET=%~1

echo ==================================================
echo    ֪ʶ  Google   ͬ
echo    %date% %time%
echo ==================================================
echo.

if not exist "%~dp0rclone.exe" goto :noexe
if not exist "%~dp0rclone.conf" goto :noconf

echo   ...
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $t = $c.BeginConnect('www.google.com', 443, $null, $null); if ($t.AsyncWaitHandle.WaitOne(4000)) { $c.EndConnect($t); $c.Close(); exit 0 } else { $c.Close(); exit 1 } } catch { exit 1 }"
if errorlevel 1 goto :nonet

set RC=%~dp0rclone.exe
set CONF=%~dp0rclone.conf
set SRC=%KB_NATIVE_DIR%
set OVL=%KB_OVERLAY_DIR%
set DEST=gdrive:֪ʶⱸ

echo.
echo   [1/5] ʼ cusnote
"%RC%" copy "%SRC%\cusnote" "%DEST%/cusnote" --config "%CONF%" --transfers 8 --checkers 16 --progress --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
echo.
echo   [2/5] ͼ cloudthumb
"%RC%" copy "%SRC%\cloudthumb" "%DEST%/cloudthumb" --config "%CONF%" --transfers 8 --checkers 16 --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
echo.
echo   [3/5] ͼ notethumb
"%RC%" copy "%SRC%\notethumb" "%DEST%/notethumb" --config "%CONF%" --transfers 8 --checkers 16 --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
echo.
echo   [4/5] ݿ db
"%RC%" copy "%SRC%\db" "%DEST%/db" --config "%CONF%" --transfers 8 --checkers 16 --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
echo.
echo   [5/5] ֻ˱ʼ
"%RC%" copy "%OVL%" "%DEST%/ֻ˱ʼ" --config "%CONF%" --transfers 8 --checkers 16 --stats 30s --log-file "%~dp0sync-log.txt" --log-level NOTICE
goto :done

:noexe
echo   [] Ҳ rclone.exeȷϱ gdrive-sync ļС
goto :end

:noconf
echo   [ʾ] û Google ˺š
echo   С1-Google˺.batɵ¼
goto :end

:nonet
echo   [ʾ] ⲻ Google 磬VPN ûӡ
echo    Hide.me VPN ͬ
goto :end

:done
echo.
echo   ͬ Google ̲鿴֪ʶⱸݡļС
echo    ERROR ѱڽͼ

:end
echo.
if /i not "%QUIET%"=="q" pause
exit /b 0
