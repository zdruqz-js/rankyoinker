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

rem Seit 2.5.0 oeffnet RankYoinker als eigenes Fenster (Edge/Chrome im
rem App-Modus) statt als Browser-Tab - dieselbe Logik wie Tray und
rem Startmenue-Symbol: vry_log_server.py --open-app waehlt die Adresse
rem (http://rankyoinker/ oder http://localhost:1101/), startet den Dienst
rem bei Bedarf und faellt ohne Edge/Chrome auf den Standardbrowser zurueck.
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
