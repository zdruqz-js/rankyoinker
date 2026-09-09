@echo off
rem Der Not-Aus von Hand. Beendet ALLES: vry.exe und den Log-Server (der die
rem Overlay-Seite ausliefert). Danach ist http://vry bzw.
rem http://localhost:1101 nicht mehr erreichbar und auch die Automatik
rem "VALORANT an -> vRY an" ist aus, bis wieder start_vry.vbs laeuft.
rem
rem Nur vRY beenden und die Seite erreichbar lassen: stop_vry_hidden.vbs

echo Beende vRY und den Log-Server...

rem Zuerst freundlich ueber den Dienst (beendet sich danach selbst)
powershell -NoProfile -Command "try { Invoke-WebRequest 'http://127.0.0.1:1101/shutdown' -UseBasicParsing -TimeoutSec 5 | Out-Null } catch {}" >nul 2>&1

timeout /t 2 >nul

rem Sicherheitshalber hart nachfassen
taskkill /F /T /IM vry.exe >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*vry_log_server*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo Fertig - es laeuft nichts mehr.
timeout /t 2 >nul
