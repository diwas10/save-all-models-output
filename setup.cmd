@echo off
rem Windows:  setup.cmd [data folder]   (options: --uninstall --dry-run)
where py >nul 2>&1
if %errorlevel%==0 (py -3 "%~dp0scripts\setup.py" %*) else (python "%~dp0scripts\setup.py" %*)
