@echo off
REM Keep Lockstep running on Windows.
REM
REM This is the stand-in for Docker's "restart: always" on the VPS. If the
REM process dies -- crash, network wobble, killed by something -- it comes back
REM in ten seconds. A commitment that quietly ends because a process exited is
REM the failure this whole project exists to prevent.
REM
REM Launched hidden at logon by run-hidden.vbs via Task Scheduler.

cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

:loop
echo. >> lockstep.log
echo ==== starting %DATE% %TIME% ==== >> lockstep.log
".venv\Scripts\python.exe" -m server.main >> lockstep.log 2>&1
echo ==== exited %DATE% %TIME%, restarting in 10s ==== >> lockstep.log
timeout /t 10 /nobreak > nul
goto loop
