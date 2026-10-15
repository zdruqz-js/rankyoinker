@echo off
setlocal EnableExtensions

rem Oeffnet die Overlay-Seite (seit 2.5.0 als App-Fenster). Wird NICHT von
rem install_all.py aus aufgerufen, sondern erst von der .iss als
rem "postinstall"-Run-Eintrag - also erst, wenn der Nutzer die "Fertig"-
rem Seite des Installer-Assistenten bestaetigt, nicht schon waehrend
rem install_all.py noch mitten im Setup-Fortschritt laeuft.
rem
rem Laeuft nie interaktiv - kein Tastendruck noetig, das Fenster (falls
rem ueberhaupt sichtbar) schliesst sich von selbst.

set "DEBUGLOG=%TEMP%\rankyoinker_install_log.txt"
call :log "=== open_overlay.bat gestartet ==="

rem Dieselbe Logik wie Tray und Startmenue-Symbol: vry_log_server.py
rem --open-app waehlt die Adresse (http://rankyoinker/ oder
rem http://localhost:1101/), startet den Dienst bei Bedarf und oeffnet die
rem Seite im Standardbrowser.
set "APPDIR=%~dp0.."
if exist "%APPDIR%\pythonw.exe" (
  call :log "  oeffne App-Fenster ueber vry_log_server.py --open-app"
  start "" "%APPDIR%\pythonw.exe" "%APPDIR%\vry_log_server.py" --open-app
) else (
  call :log "  pythonw.exe fehlt -> oeffne http://localhost:1101/ im Standardbrowser"
  start "" "http://localhost:1101/"
)

call :log "=== open_overlay.bat fertig, exit 0 ==="
exit /b 0

rem ============================================================
:log
>> "%DEBUGLOG%" echo [%date% %time%] %~1
exit /b 0
