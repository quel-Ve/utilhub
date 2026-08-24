@echo off
rem UtilityHub - stop daemon (via pid file) and delete scheduled task
setlocal
set TASK=UtilityHub
set PIDFILE=%~dp0logs\hub.pid

rem 标记正常卸载, 看门狗据此不弹"闪退"提示 (taskkill /f 退出码非 0)
echo clean > "%~dp0logs\hub.clean_exit" 2>nul
if exist "%PIDFILE%" (
    set /p PID=<"%PIDFILE%"
    taskkill /f /pid %PID% >nul 2>&1
    del "%PIDFILE%" >nul 2>&1
)
schtasks /delete /tn "%TASK%" /f >nul 2>&1
echo [OK] UtilityHub uninstalled.
