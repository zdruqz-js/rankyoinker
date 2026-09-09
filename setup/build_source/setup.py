import sys
from cx_Freeze import setup, Executable

# with open("requirements.txt", "r") as f:
#     requirements = f.read().splitlines()
#     packages = []
#     for r in requirements:
#         if "==" in r:
#             packages.append(r.split("==")[0])
#         elif "~=" in r:
#             packages.append(r.split("~=")[0])
#         elif ">=" in r:
#             packages.append(r.split(">=")[0])
#         elif "<=" in r:
#             packages.append(r.split("<=")[0])

build_exe_options = {
    "path": sys.path,
    "packages": ["requests", "colr", "InquirerPy", "websockets", "pypresence", "nest_asyncio", "rich", "websocket_server"],
    "excludes": ["tkinter", "test", "unittest", "pygments", "xmlrpc"]
}

# version ist nur das cx_Freeze-Paket-Metadatenfeld, nicht sicherheitsrelevant -
# bei jedem Release zusammen mit CURRENT_VERSION (index.html), MyAppVersion
# (RankYoinker.iss) und APP_VERSION (vry_log_server.py) hochzaehlen.
setup(
    name = "RankYoinker",
    version = "2.0.5-dev",
    description='RankYoinker - Overlay & Automation fuer VALORANT (basiert auf vRY)',
    executables = [Executable("main.py", icon="../rankyoinker.ico", target_name="vry.exe")],
    options={"build_exe": build_exe_options}
)
