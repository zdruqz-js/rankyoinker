@echo off
rem RankYoinker - baut eine neue vry.exe aus diesem Quellcode und legt sie
rem direkt an der richtigen Stelle ab (vry\vry.exe + vry\lib\). Nur auf
rem Windows lauffaehig, braucht Python 3.10 (siehe README.md/LIESMICH.txt).
rem Einfach per Doppelklick starten, laeuft aus jedem Ordner heraus.
setlocal
cd /d "%~dp0"

echo === RankYoinker: vry.exe wird neu gebaut ===
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo FEHLER: "python" wurde nicht gefunden. Python 3.10 installieren und
    echo dabei "Add python.exe to PATH" anhaken, dann nochmal versuchen.
    goto :error
)

echo Installiere/aktualisiere benoetigte Pakete...
python -m pip install --upgrade cx_Freeze requests colr InquirerPy websockets pypresence nest_asyncio rich websocket_server
if errorlevel 1 goto :error

if exist build rmdir /s /q build

echo.
echo Baue vry.exe (kann 1-2 Minuten dauern)...
python setup.py build
if errorlevel 1 goto :error

set OUTDIR=
for /d %%D in (build\exe.win-amd64-*) do set OUTDIR=%%D
if not defined OUTDIR (
    echo FEHLER: Build-Output-Ordner nicht gefunden ^(build\exe.win-amd64-*^).
    goto :error
)

echo.
echo Kopiere Ergebnis aus %OUTDIR% nach vry\ ...
copy /y "%OUTDIR%\vry.exe" "..\..\vry.exe" >nul
if errorlevel 1 goto :error
xcopy "%OUTDIR%\lib" "..\..\lib\" /E /Y /I >nul
if errorlevel 1 goto :error

echo.
echo ================================================
echo  Fertig! vry\vry.exe und vry\lib\ sind aktuell.
echo  Naechster Schritt: vry.exe + lib\ an Claude
echo  schicken (z.B. als Zip), damit der Installer
echo  (RankYoinker-Setup.exe) damit neu gebaut wird.
echo ================================================
pause
exit /b 0

:error
echo.
echo FEHLER beim Bauen - siehe Meldung oben.
pause
exit /b 1
