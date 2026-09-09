@echo off
rem Baut eine neue vry.exe aus dem Quellcode. Nur auf Windows lauffaehig.
rem Siehe build_exe.py fuer Voraussetzungen (Python 3.10, cx_Freeze, main.py).
cd /d "%~dp0"
python build_exe.py build
pause
