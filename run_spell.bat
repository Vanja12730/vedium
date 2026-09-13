REM Copyright (C) 2026 vehab
REM SPDX-License-Identifier: GPL-3.0-only

@echo off
cd /d "%~dp0"
rem Interpreter preference:
rem   1. portable Python bundled by the installer (python\ next to this script)
rem   2. development bundle (python_embed\ in a source/build checkout)
rem   3. Python 3.12 via the py launcher
rem   4. the py launcher's default Python
rem   5. whatever python is on PATH (last resort; PATH can be hijacked by venvs)
if exist "%~dp0python\python.exe" (
    "%~dp0python\python.exe" -m vedium
    goto :done
)
if exist "%~dp0python_embed\python.exe" (
    "%~dp0python_embed\python.exe" -m vedium
    goto :done
)
py -3.12 -V >nul 2>nul
if %errorlevel%==0 (
    py -3.12 -m vedium
    goto :done
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m vedium
) else (
    python -m vedium
)
:done
pause
