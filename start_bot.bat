@echo off
title SOOBRADAR Bot - 24/7 Mode

:: Prevent Windows from sleeping while bot runs
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

echo [OK] Sleep disabled. Bot starting...
echo [OK] To stop: close this window.

:LOOP
echo [%DATE% %TIME%] Starting SOOBRADAR prediction bot...
python D:\Project\SOOBRADAR\prediction_bot.py
echo [%DATE% %TIME%] Bot stopped or crashed. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto LOOP
