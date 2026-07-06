@echo off
cd /d "%~dp0"
rem Prefer the portable Python bundled by the installer, then the py launcher
rem (which ignores venvs that hijack PATH), then whatever python is on PATH.
if exist "%~dp0python\python.exe" (
    "%~dp0python\python.exe" -m spell_daemon
    goto :done
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m spell_daemon
) else (
    python -m spell_daemon
)
:done
pause
