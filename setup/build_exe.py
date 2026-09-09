# RankYoinker - cx_Freeze Build-Skript
# ------------------------------------------------------------------
# Baut aus dem Python-Quellcode eine neue vry.exe. MUSS auf Windows
# laufen (cx_Freeze baut fuer die Plattform, auf der es ausgefuehrt
# wird - von diesem Linux-Server aus ist das nicht moeglich).
#
# Das ausgelieferte RankYoinker-Paket enthielt vry.exe nur bereits
# fertig gebaut, nicht den unkompilierten Quellcode. Der vollstaendige
# Original-Quellcode (main.py + src/*.py, exakt Tag 2.94 - dieselbe
# Version wie das mitgelieferte vry.exe laut PKG-INFO) liegt darum
# direkt daneben in build_source/main.py und build_source/src/, von
# github.com/zayKenyon/VALORANT-rank-yoinker (ISC-Lizenz). Die drei
# eigenen Patches (presences.py, websocket.py, rpc.py aus lib/src/
# eine Ebene hoeher) sind dort bereits ueberkopiert, ersetzen also die
# Original-Dateien mit dem tatsaechlich ausgelieferten Stand.
#
# Bevor das hier laueft, brauchst du:
#   1. Windows-Rechner mit Python 3.10 (dieselbe Version wie im
#      ausgelieferten Paket - python310.dll)
#   2. pip install cx_Freeze requests colr InquirerPy websockets
#             pypresence nest_asyncio rich websocket_server
#      (die genaue Liste + Versionen stehen in build_source/setup.py,
#      dem Original-Build-Skript, requirements.txt fehlt allerdings in
#      diesem Auszug - im Zweifel "pip install -r requirements.txt"
#      aus dem vollen Original-Repo verwenden)
#   3. Dieses Skript aus dem Ordner build_source/ heraus ausfuehren
#      (also z.B. diese Datei dorthin kopieren), da main.py relative
#      Imports wie "from src.rpc import Rpc" nutzt.
#   4. Ein RankYoinker-Icon als .ico (fuer target_name/Fenster-Icon) -
#      noch nicht vorhanden, aktuell Platzhalter-Dateiname unten.
#
# Aufruf (aus build_source/):  python ..\build_exe.py build
# Ergebnis liegt danach unter build\exe.win-amd64-3.10\vry.exe
# ------------------------------------------------------------------

import sys
from cx_Freeze import setup, Executable

ENTRY_POINT = "main.py"
ICON_PATH = "rankyoinker.ico"  # TODO: eigenes Icon hier ablegen, sobald vorhanden

build_exe_options = {
    "path": sys.path,
    "include_files": ["configurator.bat", "updatescript.bat"],
    "packages": [
        "requests", "colr", "InquirerPy", "websockets", "pypresence",
        "nest_asyncio", "rich", "websocket_server",
    ],
    "excludes": ["tkinter", "test", "unittest", "pygments", "xmlrpc"],
}

setup(
    name="RankYoinker",
    version="1.0.0",  # bei jedem Release hochzaehlen, siehe auch version.json auf dem Server
    description="RankYoinker - Overlay & Automation fuer VALORANT (basiert auf vRY)",
    executables=[Executable(ENTRY_POINT, icon=ICON_PATH, target_name="vry.exe")],
    options={"build_exe": build_exe_options},
)
