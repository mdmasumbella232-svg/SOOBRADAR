@echo off
:: This script registers SOOBRADAR bot to auto-start on Windows boot
:: Run this ONCE as Administrator

set "BOT_BAT=D:\Project\SOOBRADAR\start_bot.bat"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP_DIR%\SOOBRADAR_Bot.bat"

copy "%BOT_BAT%" "%SHORTCUT%" >nul

if exist "%SHORTCUT%" (
    echo [OK] SOOBRADAR bot registered to run on Windows startup.
    echo      Shortcut placed at: %SHORTCUT%
) else (
    echo [ERROR] Failed to register. Try running as Administrator.
)
pause
