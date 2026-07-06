@echo off
cd /d "%~dp0"
rem Interpreter preference:
rem   1. portable Python bundled by the installer (python\ next to this script)
rem   2. Python 3.12 via the py launcher — the prebuilt GPU llama-cpp-python
rem      wheel is a cp312 build, so local GGUF models need 3.12
rem   3. the py launcher's default Python (cloud / llama-server modes work fine)
rem   4. whatever python is on PATH (last resort; PATH can be hijacked by venvs)
if exist "%~dp0python\python.exe" (
    "%~dp0python\python.exe" -m spell_daemon
    goto :done
)
py -3.12 -V >nul 2>nul
if %errorlevel%==0 (
    py -3.12 -m spell_daemon
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
