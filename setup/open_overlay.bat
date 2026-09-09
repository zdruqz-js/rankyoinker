@echo off
setlocal EnableExtensions

rem Oeffnet die Overlay-Seite im Standardbrowser. Wird NICHT von
rem install_all.py aus aufgerufen, sondern erst von der .iss als
rem "postinstall"-Run-Eintrag - also erst, wenn der Nutzer die "Fertig"-
rem Seite des Installer-Assistenten bestaetigt, nicht schon waehrend
rem install_all.py noch mitten im Setup-Fortschritt laeuft.
rem
rem Laeuft nie interaktiv - kein Tastendruck noetig, das Fenster (falls
rem ueberhaupt sichtbar) schliesst sich von selbst.

set "DEBUGLOG=%TEMP%\rankyoinker_install_log.txt"
call :log "=== open_overlay.bat gestartet ==="

rem RankYoinker bringt sein eigenes eingebettetes Python mit (siehe
rem install_all.py Schritt 0) - kein Python-Check mehr noetig, der
rem Log-Server laeuft in jedem Fall.

rem Der Log-Server bindet Port 1101 zuerst und Port 80 (fuer die kurze
rem Adresse http://vry) erst danach als zweiten, separaten Server. Bis
rem hierher ist er zwar schon eine Weile gelaufen (install_all.py hat
rem ihn selbst gestartet), zur Sicherheit trotzdem kurz auf Port 80
rem pollen statt blind zu verlinken - und danach IMMER nur genau EINEN
rem Tab oeffnen (nie erst localhost, dann vry).
set "PORT80READY="
for /L %%N in (1,1,6) do (
  if not defined PORT80READY (
    powershell -NoProfile -Command "try { $r = Invoke-WebRequest 'http://127.0.0.1/status' -UseBasicParsing -TimeoutSec 1; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
    if not errorlevel 1 (set "PORT80READY=1") else (ping -n 2 127.0.0.1 >nul)
  )
)

if defined PORT80READY (
  call :log "  Port 80 bereit -> oeffne http://vry/"
  start "" "http://vry/"
) else (
  call :log "  Port 80 nicht bereit/kein hosts-Eintrag -> oeffne http://localhost:1101/"
  start "" "http://localhost:1101/"
)

call :log "=== open_overlay.bat fertig, exit 0 ==="
exit /b 0

rem ============================================================
:log
>> "%DEBUGLOG%" echo [%date% %time%] %~1
exit /b 0
