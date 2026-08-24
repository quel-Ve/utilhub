@echo off
rem UtilityHub - register login scheduled task (highest privilege) for unified hotkey daemon
rem Supersedes: 6TaskbarSortTool TaskbarSortDaemon + zorder-snapshot HKCU Run key
setlocal
set TASK=UtilityHub
set HUBROOT=%~dp0
set HUB=%HUBROOT%hub.py

rem Locate real pythonw.exe (skip WindowsApps store stub)
set PYTHONW=
for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYTHONW (
        if exist "%%~dpi..\pythonw.exe" set PYTHONW=%%~dpi..\pythonw.exe
    )
)
if not defined PYTHONW (
    if exist "D:\Program Files\python\python1210\pythonw.exe" set PYTHONW=D:\Program Files\python\python1210\pythonw.exe
)
if not defined PYTHONW (
    echo [ERROR] pythonw.exe not found. Install Python and add it to PATH.
    exit /b 1
)

rem Native injection components must be built in sibling project
if not exist "%HUBROOT%..\6TaskbarSortTool\native\build\bin\Injector.exe" (
    echo [ERROR] 6TaskbarSortTool\native\build\bin\Injector.exe missing. Build first:
    echo         cd 6TaskbarSortTool ^&^& cmake -B native/build -G "MinGW Makefiles" ^&^& cmake --build native/build
    exit /b 1
)

rem Retire legacy autostarts (hotkey listeners must not duplicate)
taskkill /f /im zorder-snapshot.exe >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v zorder-snapshot /f >nul 2>&1
schtasks /delete /tn TaskbarSortDaemon /f >nul 2>&1
if exist "%HUBROOT%..\6TaskbarSortTool\logs\hotkey_daemon.pid" (
    set /p OLDPID=<"%HUBROOT%..\6TaskbarSortTool\logs\hotkey_daemon.pid"
    taskkill /f /pid %OLDPID% >nul 2>&1
    del "%HUBROOT%..\6TaskbarSortTool\logs\hotkey_daemon.pid" >nul 2>&1
)
rem Retire legacy EQ daemon (21eq-switcher venv pythonw + task) — hub 已内置
schtasks /delete /tn EQSwitcher /f >nul 2>&1
wmic process where "name='pythonw.exe' and ExecutablePath like '%%21eq-switcher%%'" call terminate >nul 2>&1

schtasks /create /tn "%TASK%" /tr "\"%PYTHONW%\" \"%HUB%\" --daemon" /sc onlogon /ru %USERNAME% /rl highest /f
if errorlevel 1 (
    echo [ERROR] Failed to register scheduled task.
    exit /b 1
)
schtasks /run /tn "%TASK%" >nul 2>&1
echo [OK] UtilityHub installed and started.
echo     Hotkeys: Ctrl+Alt+` sort / Ctrl+Alt+1/2/3 snapshot-restore. Uninstall: uninstall.bat
