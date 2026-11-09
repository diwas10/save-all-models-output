@echo off
rem Windows:  setup.cmd   (options: --uninstall --dry-run)
where py >/dev/null 2>&1
if %errorlevel%==0 (py -3 "%~dp0scripts\setup.py" %*) else (python "%~dp0scripts\setup.py" %*)
