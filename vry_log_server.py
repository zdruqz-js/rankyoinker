"""Log-Server + Riot-API-Proxy fuer das RankYoinker Browser-Overlay.

Liefert an index.html:
  /log      Konsolen-Log von vry.exe + Ladefortschritt
  /status   laeuft vry.exe? + Ladefortschritt
  /start    startet vry.exe unsichtbar
  /stop     beendet ALLES (vry.exe, andere Log-Server, sich selbst)
  /api/session          Auth-Status, eigene puuid, Region
  /api/kda?puuids=...   K/D/A aus dem letzten Match je Spieler
  /api/player?puuid=..  ausfuehrliche Statistik ueber die letzten Matches
  /api/dodge            verlaesst die laufende Agentenauswahl (Queue dodgen)
  /api/gamestate        echter Spielzustand laut Riot (MENUS/PREGAME/INGAME)
  /api/instalock        Instalock scharf schalten/abfragen (laeuft serverseitig)
  /api/pair             Kopplung fuer Handys im selben Netz (QR-Code, Geraeteliste)

Der Dienst hoert auf allen Netzwerkkarten, damit die Seite auch vom Handy im
selben WLAN erreichbar ist. Weil hier echte Aktionen mit dem Riot-Account
haengen (dodgen, Instalock, Party), ist alles ausser dem PC selbst gesperrt:
Ein fremdes Geraet kommt nur mit einem gekoppelten Geraete-Token rein, den es
einmalig ueber den QR-Code auf dem PC bekommt. Vom PC (127.0.0.1) geht wie
bisher alles ohne Kopplung.

vry.exe wird bewusst NICHT blind mitgestartet: Ohne laufendes VALORANT haengt
es in "Connection error, retrying", und ohne Region im Spiel-Log bleibt es
stumm nach "opened lockfile" stehen. Ein Startwunsch wird darum vorgemerkt und
ausgefuehrt, sobald beides bereit ist. Zwei Waechter halten es danach am
Laufen: Der eine vergleicht den echten Spielzustand mit dem, was vry.exe sieht
(damit ein laufendes Match ohne Handstart erscheint), der andere erkennt ein
Log, das nicht mehr waechst, und startet den Haenger neu.

Die Riot-Endpunkte brauchen die Token des lokalen Riot Clients — dieselbe
Kette, die vry.exe auch nutzt: Lockfile -> Entitlements -> pd/glz-Hosts.
Laeuft unsichtbar via pythonw.exe, beendet sich, wenn der Port belegt ist.
"""
import base64
import ctypes
import glob
import gzip
import hashlib
import hmac
import ipaddress
import collections
import itertools
import json
import math
import os
import random
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
# Eine .json pro Sprache (de.json, pl.json, ...) statt fest im JS eingebettet -
# lassen sich so einzeln an Uebersetzer weitergeben, und eine neue Sprache
# hinzuzufuegen heisst nur "eine weitere Datei reinlegen", kein Code-Aenderung.
# Siehe notesForCurrentLang()/LANGUAGES in index.html fuer die Verwendung.
LANG_DIR = os.path.join(BASE, "lang")
_LANG_CODE_RE = re.compile(r"^[a-z]{2}$")
PORT = 1101
CREATE_NO_WINDOW = 0x08000000

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
LOCKFILE = os.path.join(LOCALAPPDATA, "Riot Games", "Riot Client", "Config", "lockfile")
SHOOTER_LOG = os.path.join(LOCALAPPDATA, "VALORANT", "Saved", "Logs", "ShooterGame.log")

# Vom Programmordner (BASE = {app}, seit 2.4.2 Program Files) getrennter, garantiert vom
# aktuell eingeloggten Nutzer OHNE Sonderrechte beschreibbarer Ordner fuer
# alles, was zur Laufzeit entsteht (config.json, Presets, Kopplung, Logs,
# der heruntergeladene Update-Installer, ...).
#
# Vorher lag all das direkt in BASE, mit "Permissions: users-modify" auf dem
# GANZEN Ordner in RankYoinker.iss - also fuer JEDE lokal angemeldete Person
# beschreibbar, nicht nur den Installierenden. Kombiniert mit dem admin-
# elevierten Uninstaller, der python.exe/vry_log_server.py genau aus diesem
# Ordner ausfuehrt (siehe [UninstallRun] in RankYoinker.iss), war das eine
# echte Rechteausweitung: auf einer Mehrnutzer-Maschine konnte JEDE Person
# mit einem gewoehnlichen Standardkonto dort Code platzieren, der beim
# naechsten (admin-elevierten) Deinstallieren durch irgendjemand anderen mit
# Adminrechten ausgefuehrt worden waere (Community-Sicherheitsbericht,
# 2026-09-28). BASE ist seitdem nur noch fuer die eigentlichen Programm-
# dateien gedacht (vry.exe, pythonw.exe, *.dll/*.pyd, lib/, index.html,
# lang/, dieses Skript selbst) und hat KEINE "users-modify"-Berechtigung
# mehr - siehe [Dirs] in RankYoinker.iss.
DATA_DIR = os.path.join(LOCALAPPDATA or BASE, "RankYoinker")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass
LOGDIR = os.path.join(DATA_DIR, "logs")


def _migrate_legacy_data():
    """Einmaliger Umzug: bestehende Installationen (vor dem Sicherheitsfix
    2026-09-28) haben ihre Laufzeitdaten noch in BASE liegen. Kopiert jede
    bekannte Datei/den logs-Ordner genau EINMAL nach DATA_DIR hinueber, wenn
    sie dort noch fehlt - niemand soll seine gespeicherten Presets, die
    Handy-Kopplung oder die Spracheinstellung durch dieses Update verlieren.
    Loescht die alten Dateien bewusst NICHT (BASE ist fuer normale Nutzer ab
    jetzt ohnehin nur noch lesbar, sie wuerden gleich wieder als "vorhanden,
    aber nicht mehr benutzt" daliegen bleiben - kein Grund, das Loeschen zu
    riskieren, falls beim Kopieren doch mal was schiefgeht)."""
    legacy_names = [
        ".rankyoinker_client_id", ".rankyoinker_feature_counts.json",
        ".rankyoinker_last_report.json", ".rankyoinker_matches_tracked_seen.json",
        "config.json", "vry_presets.json", "vry_encounters.json",
        "vry_instalock.json", "vry_pair.json", "vry_lol.json",
        "vry_lol_presets.json", "vry_lol_encounters.json",
    ]
    # Seit 2.4.2 liegt das Programm in Program Files; der Installer räumt den
    # alten Ordner C:\ProgramData\RankYoinker beim Umzug leer, lässt genau
    # diese Altdaten aber liegen - also dort ebenfalls nachsehen.
    legacy_dirs = [BASE, os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "RankYoinker")]
    for name in legacy_names:
        dst = os.path.join(DATA_DIR, name)
        for d in legacy_dirs:
            src = os.path.join(d, name)
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    pass
    legacy_logs = os.path.join(BASE, "logs")
    if os.path.isdir(legacy_logs) and not os.path.isdir(LOGDIR):
        try:
            shutil.copytree(legacy_logs, LOGDIR)
        except OSError:
            pass


_migrate_legacy_data()

# Standard-Plattform-Header des VALORANT-Clients (base64-JSON)
CLIENT_PLATFORM = (
    "ewogICAgICAgICJwbGF0Zm9ybVR5cGUiOiAiUEMiLAogICAgICAgICJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA"
    "ogICAgICAgICJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwKICAgICAgICAi"
    "cGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iCiAgICB9"
)

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


# ============================ Prozess-Steuerung ============================

def newest_log():
    files = glob.glob(os.path.join(LOGDIR, "log-*.txt"))
    return max(files, key=os.path.getmtime) if files else None


# vry.exe bis 2.4.5-dev3 schrieb beim Token-Holen den lokalen Auth-Header
# (Basic riot:<Lockfile-Passwort>) ins Log - und /log gibt das Log auch an
# gekoppelte Handys. Neue Logs enthalten ihn nicht mehr; aeltere Dateien werden
# hier vor der Ausgabe geschwaerzt.
# {20,}: echte Werte sind deutlich laenger (Basic ~36 Zeichen, Bearer-JWT >100);
# mit {8,} wurde auch der Kartenname "Basic Training" geschwaerzt.
_LOG_SECRET_RE = re.compile(r"((?:Basic|Bearer)\s+)[A-Za-z0-9+/=._\-]{20,}")


def _redact_log(text):
    return _LOG_SECRET_RE.sub(r"\1***", text)


def vry_pids():
    """PIDs aller laufenden vry.exe — Grundlage fuer 'es darf nur eine geben'."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq vry.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return []
    pids = []
    for line in (out or "").splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "vry.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def vry_running():
    return bool(vry_pids())


def _kill_pids(pids):
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass


def _kill_all_vry():
    try:
        subprocess.run(["taskkill", "/F", "/T", "/IM", "vry.exe"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def _kill_other_log_servers():
    """Killt jede ANDERE pythonw.exe/python.exe-Instanz, die vry_log_server.py
    ausfuehrt - nicht die eigene (self.PID wird ausgenommen). Ein Update killt
    bisher nur den eigenen, gerade laufenden Prozess und wartet, bis GENAU
    DER weg ist - eine zweite, unabhaengige Instanz aus einer frueheren, nie
    sauber beendeten Sitzung wuerde davon nie beruehrt und ueberlebt jedes
    Update unbemerkt. Das faengt genau diesen Fall zusaetzlich ab."""
    my_pid = os.getpid()
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') "
        "-and $_.CommandLine -like '*vry_log_server.py*' -and $_.ProcessId -ne %d "
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    ) % my_pid
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)
    except Exception:
        pass


# ---- Läuft VALORANT überhaupt? ----
# vry.exe direkt beim Programmstart hochzufahren, während weder Riot Client
# noch VALORANT laufen, endet in "Connection error, retrying" — und aus dieser
# Schleife findet vry.exe nicht zuverlässig wieder heraus. Darum wird ein
# Startwunsch nur gemerkt und erst ausgeführt, wenn das Spiel erreichbar ist.

VALORANT_PROC = "VALORANT-Win64-Shipping.exe"   # das Spiel selbst
VALORANT_LAUNCHER = "VALORANT.exe"              # kleiner Starter, laeuft daneben
_task_cache = {}          # Prozessname -> (Zeitpunkt, laeuft?)
TASK_TTL = 2.0            # tasklist ist teuer, 2 Sekunden Gedaechtnis reichen


def _image_running(name):
    now = time.time()
    hit = _task_cache.get(name)
    if hit and now - hit[0] < TASK_TTL:
        return hit[1]
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % name, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        ).stdout or ""
    except Exception:
        return False
    running = name.lower() in out.lower()
    _task_cache[name] = (now, running)
    return running


def game_ready():
    """VALORANT laeuft UND der Riot Client haelt ein Lockfile bereit.

    Genau das braucht vry.exe. Ist eines von beidem nicht da, wird nichts
    gestartet — weder von Hand noch vom Watchdog.
    """
    return _image_running(VALORANT_PROC) and os.path.exists(LOCKFILE)


def valorant_present():
    """Ist VALORANT ueberhaupt offen? Der Starter VALORANT.exe erscheint zuerst
    und bleibt danebenliegen — beides zaehlt, damit waehrend des Ladens nichts
    voreilig beendet wird."""
    return _image_running(VALORANT_PROC) or _image_running(VALORANT_LAUNCHER)


# ---- Steht die Region schon im Spiel-Log? ----
# Direkt nach "opened lockfile" liest vry.exe die glz-URL aus ShooterGame.log.
# VALORANT legt diese Datei bei jedem Spielstart NEU an (die alte wandert nach
# ShooterGame-backup-*.log). Startet vry.exe in genau diesem Moment, liest es
# ein noch leeres Log — und kommt aus der Lese-Schleife nicht mehr heraus.
# Darum wird vorher geprüft, ob die Region überhaupt schon dasteht.
#
# Das Log wird dabei nur einmal vorwärts durchgelesen: gemerkt wird die
# Leseposition, danach kommen nur noch die neu angehängten Bytes dran. Sonst
# wäre die Prüfung bei einer 27-MB-Datei zu teuer für den Sekundentakt.

_shooter_lock = threading.Lock()
_shooter_scan = {"key": None, "pos": 0, "ok": False}
SCAN_OVERLAP = 200        # Ueberlappung, damit keine URL an der Nahtstelle zerfaellt


def region_ready():
    """Enthaelt ShooterGame.log der LAUFENDEN Sitzung bereits eine glz-URL?"""
    try:
        st = os.stat(SHOOTER_LOG)
    except OSError:
        return False
    # Neue Datei (anderer Erstellzeitpunkt) oder geschrumpft -> neue Sitzung,
    # alles Gemerkte ist damit hinfällig.
    key = (st.st_ctime, )
    with _shooter_lock:
        s = _shooter_scan
        if s["key"] != key or st.st_size < s["pos"]:
            s.update({"key": key, "pos": 0, "ok": False})
        if s["ok"] or st.st_size <= s["pos"]:
            return s["ok"]
        pos = s["pos"]
    try:
        with open(SHOOTER_LOG, "rb") as fh:
            fh.seek(pos)
            chunk = fh.read()
    except OSError:
        return False
    found = bool(RE_GLZ.search(chunk.decode("utf-8", "replace")))
    with _shooter_lock:
        s = _shooter_scan
        # Hat inzwischen eine neue Sitzung angefangen, ist das Gelesene wertlos
        # — dann lieber einmal "noch nicht" melden und beim nächsten Mal frisch
        # nachsehen, statt eine alte Region gelten zu lassen.
        if s["key"] != key:
            return False
        s["pos"] = max(pos, pos + len(chunk) - SCAN_OVERLAP)
        s["ok"] = s["ok"] or found
        return s["ok"]


def vry_ready():
    """Alles da, was vry.exe zum Start braucht: Spiel, Lockfile UND Region.

    game_ready() allein reicht nicht: Das Lockfile liegt schon, bevor das Spiel
    seine Region ins Log geschrieben hat. Wer in diesem Fenster startet, haengt
    hinterher stumm nach 'opened lockfile' fest.
    """
    return game_ready() and region_ready()


# ---- Prozess-Zustand: genau EINE vry.exe, egal wie oft geklickt wird ----

_proc_lock = threading.RLock()
_proc = {
    "desired": False,        # soll vry.exe laufen? (nur Start setzt das)
    "pid": None,             # von uns gestartete Instanz
    "last_start": 0.0,       # Zeitpunkt des letzten Startbefehls
    "last_stop": 0.0,
    "misses": 0,             # wie oft der Prozess in Folge fehlte
    "restarts": 0,           # Auto-Neustarts nach einem Absturz
    "last_restart": 0.0,
    "note": "",              # letzte Meldung fuer die Oberflaeche
    "giveup": False,         # zu viele Abstuerze -> nicht weiter neu starten
    "armed": False,          # Start vorgemerkt, wartet auf VALORANT
    "auto": True,            # vry.exe folgt VALORANT (an und aus)
    "gone": 0,               # wie oft VALORANT in Folge fehlte
}
GONE_TICKS = 2               # so oft muss VALORANT fehlen, bevor vRY zugeht
START_GRACE = 8.0            # so lange gilt ein Startbefehl als "unterwegs"
STOP_GRACE = 2.0             # Spam-Schutz fuer Stop
WATCH_INTERVAL = 3.0
MAX_RESTARTS = 5             # innerhalb von RESTART_WINDOW
RESTART_WINDOW = 900.0


def _enforce_single(pids=None):
    """Mehr als eine vry.exe? Alle ueberzaehligen beenden.

    Bevorzugt bleibt die Instanz am Leben, die wir selbst gestartet haben —
    sonst die mit der kleinsten PID (stabile, reproduzierbare Wahl).
    """
    pids = vry_pids() if pids is None else pids
    if len(pids) < 2:
        return 0
    keep = _proc.get("pid") if _proc.get("pid") in pids else min(pids)
    extra = [p for p in pids if p != keep]
    _kill_pids(extra)
    _proc["pid"] = keep
    _proc["note"] = "%d doppelte RankYoinker-Instanz(en) beendet." % len(extra)
    return len(extra)


def start_vry(force=False):
    """Startet vry.exe — mehrfaches Klicken erzeugt keine zweite Instanz."""
    with _proc_lock:
        now = time.time()
        _proc["desired"] = True
        _proc["giveup"] = False
        _proc["auto"] = True     # ein Start hebt einen frueheren Hart-Stopp auf
        _proc["gone"] = 0
        # Ein Startbefehl ist noch unterwegs: der Prozess taucht in tasklist
        # erst nach ein paar hundert Millisekunden auf — bis dahin ignorieren,
        # sonst startet jeder weitere Klick eine weitere Instanz.
        if not force and now - _proc["last_start"] < START_GRACE:
            return {"ok": True, "started": False, "reason": "pending",
                    "running": True}
        pids = vry_pids()
        if pids:
            _proc["armed"] = False
            killed = _enforce_single(pids)
            _proc["last_start"] = now
            return {"ok": True, "started": False, "reason": "already_running",
                    "running": True, "killedDuplicates": killed}
        # Kein VALORANT (oder Region noch nicht im Spiel-Log) -> Start nur
        # vormerken. Der Watchdog holt ihn nach, sobald alles bereit ist; so
        # läuft vry.exe nie ins Leere und hängt sich auch nicht auf.
        if not vry_ready():
            _proc["armed"] = True
            _proc["note"] = ("VALORANT laeuft noch nicht — RankYoinker startet automatisch, "
                             "sobald das Spiel erreichbar ist."
                             if not game_ready() else
                             "VALORANT startet noch — RankYoinker wartet auf die Region "
                             "im Spiel-Log und startet dann von selbst.")
            return {"ok": True, "started": False, "reason": "waiting_game",
                    "waiting": True, "running": False}
        _proc["armed"] = False
        try:
            proc = subprocess.Popen([os.path.join(BASE, "vry.exe")], cwd=BASE,
                                    creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            _proc["note"] = "Start fehlgeschlagen: %s" % e
            return {"ok": False, "error": str(e), "running": False}
        _proc["pid"] = proc.pid
        _proc["last_start"] = now
        _proc["misses"] = 0
        _proc["note"] = ""
        return {"ok": True, "started": True, "running": True, "pid": proc.pid}


def _kill_other_servers():
    """Doppelte Log-Server-Instanzen beenden — diese hier bleibt am Leben."""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*vry_log_server*' -and "
        "$_.ProcessId -ne %d } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        % os.getpid()
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def stop_vry_only():
    """Beendet vry.exe samt Kindprozessen und doppelte Log-Server.

    Dieser Prozess laeuft bewusst WEITER: Er liefert die Seite aus
    (http://localhost:1101 bzw. http://vry). Wuerde er sich mitbeenden,
    waere die Seite danach nicht mehr erreichbar und der Start-Button
    damit unbenutzbar.

    Mehrfaches Klicken ist harmlos: Der Stopp-Wunsch wird gemerkt, der
    Watchdog startet danach nichts mehr von selbst.
    """
    with _proc_lock:
        now = time.time()
        _proc["desired"] = False
        _proc["pid"] = None
        _proc["misses"] = 0
        _proc["armed"] = False       # vorgemerkter Start ist damit hinfaellig
        # Ein ausdrücklicher Stopp (stop_vry.bat) hält auch die Automatik an,
        # sonst würde der Watchdog vry.exe sofort wieder hochziehen.
        _proc["auto"] = False
        # Kurz hintereinander geklickt -> der Kill läuft schon, nichts doppelt tun
        if now - _proc["last_stop"] < STOP_GRACE:
            return {"ok": True, "running": vry_running(), "reason": "pending"}
        _proc["last_stop"] = now
    _kill_all_vry()
    _kill_other_servers()
    return {"ok": True, "running": vry_running()}


def stop_everything():
    """Wirklich alles beenden, inklusive dieses Prozesses (fuer stop_vry.bat)."""
    stop_vry_only()
    threading.Timer(0.8, lambda: os._exit(0)).start()


def _follow_valorant():
    """Koppelt vry.exe an VALORANT: geht mit an und wieder aus.

    Es gibt bewusst keine Start/Stop-Knoepfe mehr — VALORANT starten reicht.
    Das Beenden wird zwei Runden lang gegengeprueft, damit ein kurzer
    Aussetzer von tasklist waehrend eines Map-Ladens nichts abwuergt.
    """
    with _proc_lock:
        if not _proc["auto"]:
            return
        if valorant_present():
            _proc["gone"] = 0
            if not _proc["desired"]:
                # Erst starten, wenn das Spiel auch wirklich erreichbar ist —
                # sonst landet vry.exe wieder in "Connection error, retrying"
                # oder hängt sich beim Lesen der Region auf.
                if not vry_ready():
                    return
                _proc.update({"desired": True, "armed": True, "giveup": False,
                              "misses": 0, "restarts": 0,
                              "note": "VALORANT gestartet — RankYoinker laeuft automatisch mit."})
            return

        # VALORANT ist weg — erst nach mehreren Runden glauben
        _proc["gone"] += 1
        if _proc["gone"] < GONE_TICKS or not (_proc["desired"] or _proc["armed"]):
            return
        _proc.update({"desired": False, "armed": False, "pid": None, "misses": 0,
                      "note": "VALORANT beendet — RankYoinker wurde automatisch mitgeschlossen."})
    if vry_running():
        _kill_all_vry()


# ============================ RankYoinker-Erweiterung ============================
# Sendet alle 60s einen anonymen "Ping" an rankyoinker.de, nur damit die Website
# anzeigen kann, wie viele Leute das Tool gerade aktiv nutzen, und das Team per
# Discord /stats grobe Versions-/Sprachverteilung sehen kann - nur die zufaellige,
# pro Installation einmalig erzeugte Client-ID plus App-Version und UI-Sprache.
# Alle 5 Minuten kommt zusaetzlich ein zweiter, selteneren Bericht dazu (siehe
# _client_report_loop() weiter unten) mit dem verknuepften Riot-Account (Puuid +
# Anzeigename) und lokalen Feature-Nutzungszaehlern, fuers interne, admin-only
# Dashboard - siehe die Datenschutzerklaerung (Abschnitt "RankYoinker-Anwendung")
# fuer die Offenlegung dieser zusaetzlichen Kategorie.
# Von Hand mit CURRENT_VERSION (index.html) und MyAppVersion (RankYoinker.iss)
# synchron halten - bei jedem Release alle drei zusammen hochzaehlen.
APP_VERSION = "2.5.5-dev2"
HEARTBEAT_URL = "https://rankyoinker.de/api/heartbeat"
HEARTBEAT_INTERVAL = 60
_CLIENT_ID_PATH = os.path.join(DATA_DIR, ".rankyoinker_client_id")
_HEARTBEAT_ERR_PATH = os.path.join(DATA_DIR, "rankyoinker-heartbeat-error.txt")

# Anonymer "ein Match wurde getrackt"-Zaehler fuers oeffentliche Live-Stats-
# Embed im Discord (siehe _game_watcher()/_lol_watcher() weiter unten, wo das
# jeweils einmal pro Match ausgeloest wird) - nur "valorant" oder "league",
# sonst nichts, kein Match-/Account-Bezug.
MATCH_TRACKED_NOTIFY_URL = "https://rankyoinker.de/api/notify-match-tracked"


def _notify_match_tracked(game):
    def _send():
        try:
            body = json.dumps({"game": game}).encode("utf-8")
            req = urllib.request.Request(
                MATCH_TRACKED_NOTIFY_URL,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "RankYoinker-MatchTracked"},
                method="POST",
            )
            _urlopen_public(req, timeout=10).close()
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


# Welches Match je Spiel zuletzt gemeldet wurde - AUF DISK, nicht nur im
# Prozessspeicher: der Log-Server startet oft neu (Updates, Abstuerze), und
# eine rein prozessinterne Merkliste haette dasselbe, laengst gemeldete Match
# nach so einem Neustart nochmal als "neu" gesehen und doppelt gezaehlt.
# Nur die letzten MATCHES_TRACKED_SEEN_MAX IDs je Spiel - waechst dadurch
# nicht unbegrenzt (Match-IDs kommen ohnehin nie wieder vor, mehr als 1-2
# braeuchte es dafuer eigentlich nicht, aber ein kleiner Puffer schadet nicht).
MATCHES_TRACKED_SEEN_FILE = os.path.join(DATA_DIR, ".rankyoinker_matches_tracked_seen.json")
MATCHES_TRACKED_SEEN_MAX = 10
_matches_tracked_seen_cache = None


def _notify_match_tracked_once(game, match_id):
    """Meldet ein Match hoechstens EINMAL als getrackt (siehe MATCHES_TRACKED_SEEN_FILE
    oben fuer den Grund, warum das nicht einfach ein Dict im Speicher ist)."""
    global _matches_tracked_seen_cache
    if not match_id:
        return
    match_id = str(match_id)
    if _matches_tracked_seen_cache is None:
        data = _load_json(MATCHES_TRACKED_SEEN_FILE)
        _matches_tracked_seen_cache = data if isinstance(data, dict) else {}
    recent = _matches_tracked_seen_cache.get(game)
    if not isinstance(recent, list):
        recent = []
    if match_id in recent:
        return
    recent.append(match_id)
    _matches_tracked_seen_cache[game] = recent[-MATCHES_TRACKED_SEEN_MAX:]
    _save_json(MATCHES_TRACKED_SEEN_FILE, _matches_tracked_seen_cache)
    _notify_match_tracked(game)


# HTTPS-Requests an das ECHTE INTERNET (rankyoinker.de) - nicht zu verwechseln
# mit _SSL oben, das bewusst unverifiziert nur für die lokale LCU-API auf
# 127.0.0.1 ist. Manche (v.a. eingebettete) Python-Distributionen auf Windows
# finden das System-Zertifikatsdepot nicht zuverlässig, wodurch JEDE
# urllib-HTTPS-Anfrage mit einem Zertifikatsfehler scheitert, obwohl ein
# normaler Browser auf demselben PC (eigener, unabhängiger Zertifikats-Stack)
# klaglos funktioniert. Das blieb bisher unbemerkt, weil Fehler beim
# Heartbeat/Update-Download stillschweigend verschluckt wurden. Fix: beim
# Standardkontext bleiben, aber bei einem Zertifikatsfehler einmal mit dem
# mitgelieferten certifi-Bundle (lib/certifi/cacert.pem) erneut versuchen.
try:
    import certifi
    _CERTIFI_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _CERTIFI_CTX = None


def _urlopen_public(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        # urlopen wirft bei einem TLS-Zertifikatsfehler NICHT direkt
        # ssl.SSLCertVerificationError - http.client fängt jeden OSError
        # (SSLError ist eine Unterklasse davon) während des Verbindungsaufbaus
        # ab und urllib wickelt ihn in URLError ein (e.reason trägt den
        # eigentlichen Fehler). Der erste Versuch dieses Fixes hat genau
        # deswegen nie gegriffen - der Fallback-Zweig war totes Code.
        if _CERTIFI_CTX and isinstance(e.reason, ssl.SSLError):
            return urllib.request.urlopen(req, timeout=timeout, context=_CERTIFI_CTX)
        raise


_client_id_lock = threading.Lock()
_client_id_cache = None


def _get_client_id():
    # Heartbeat- und Report-Thread starten direkt nacheinander und fragen beide
    # sofort nach der ID. Ohne Sperre fanden beim allerersten Start beide noch
    # keine Datei und erzeugten je eine EIGENE ID - eine Installation meldete
    # sich dann bis zum nächsten Neustart unter zwei IDs (doppelt gezählt,
    # Heartbeat und Report im Dashboard nie zusammengeführt).
    global _client_id_cache
    with _client_id_lock:
        if _client_id_cache:
            return _client_id_cache
        try:
            with open(_CLIENT_ID_PATH, "r", encoding="utf-8") as f:
                existing = f.read().strip()
                if existing:
                    _client_id_cache = existing
                    return existing
        except OSError:
            pass
        new_id = secrets.token_hex(16)
        try:
            with open(_CLIENT_ID_PATH, "w", encoding="utf-8") as f:
                f.write(new_id)
        except OSError:
            pass
        _client_id_cache = new_id
        return new_id


# Primary Language ID = niedrigstes Byte einer Windows-LCID, stabile Win32-
# Konstanten (siehe "Language Identifier Constants and Strings" bei MS).
_WIN_PRIMARY_LANG = {0x07: "de", 0x09: "en", 0x15: "pl", 0x0c: "fr", 0x0a: "es", 0x1f: "tr", 0x12: "ko",
                     0x16: "pt", 0x19: "ru", 0x11: "ja"}


def _detect_system_lang():
    """Windows-Anzeigesprache als Fallback, wenn config.json noch kein "lang"
    hat - z.B. weil die Overlay-Seite noch nie im Browser geoeffnet wurde
    (das ist der einzige Weg, wie "lang" ueberhaupt hineingeschrieben wird,
    siehe notifyBackendLang() in index.html). Ohne diesen Fallback fiel das
    bisher hart auf "de" zurueck, VOELLIG unabhaengig von der tatsaechlichen
    Systemsprache - deshalb sah die Sprachverteilung in /stats verzerrt
    Richtung Deutsch aus, sobald jemand die Overlay-Seite gar nicht oder erst
    spaeter im Browser aufruft (z.B. weil nur Discord Rich Presence oder die
    Konsole genutzt wird)."""
    try:
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return _WIN_PRIMARY_LANG.get(lcid & 0xFF, "en")
    except Exception:
        return "en"


def _heartbeat_lang():
    """Wie rpc.py._current_lang(), aber eigenstaendig - kein Cross-Import
    zwischen vry_log_server.py und lib/src/rpc.py noetig fuer ein Feld."""
    try:
        data = _load_json(CONFIG_JSON_FILE) or {}
        raw = data.get("lang")
        if raw:
            code = str(raw).lower()
            if code in RPC_LANGS:
                return code
    except Exception:
        pass
    return _detect_system_lang()


def _active_user_heartbeat():
    client_id = _get_client_id()
    while True:
        try:
            # Erst hier gebaut statt einmalig vor der Schleife, damit ein
            # Sprachwechsel waehrend der Laufzeit (Einstellungen-Dropdown) im
            # naechsten Ping sofort ankommt statt erst nach einem Neustart.
            body = json.dumps({
                "clientId": client_id,
                "version": APP_VERSION,
                "lang": _heartbeat_lang(),
            }).encode("utf-8")
            req = urllib.request.Request(
                HEARTBEAT_URL,
                data=body,
                # Ohne eigenen User-Agent fällt urllib auf "Python-urllib/x.y"
                # zurück - das blockt Cloudflare mit 403 (siehe get_auth()
                # weiter unten für denselben, dort schon gelösten Fall bei
                # der VALORANT-API).
                headers={"Content-Type": "application/json", "User-Agent": "RankYoinker-Heartbeat"},
                method="POST",
            )
            _urlopen_public(req, timeout=5).close()
            try:
                os.remove(_HEARTBEAT_ERR_PATH)
            except OSError:
                pass
        except Exception as e:
            report_error("heartbeat", e)
            # Früher komplett verschluckt - ohne das war "warum zeigt die
            # Website 0 an, obwohl das Tool läuft" nicht diagnostizierbar.
            # Nur der letzte Fehler wird gehalten (überschrieben), damit die
            # Datei nicht unbegrenzt wächst.
            try:
                with open(_HEARTBEAT_ERR_PATH, "w", encoding="utf-8") as f:
                    f.write("%s: %s: %s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"), type(e).__name__, e))
            except OSError:
                pass
        time.sleep(HEARTBEAT_INTERVAL)


# ---- Feature-Nutzung + Account-Bericht alle 5 Minuten ----
# Getrennt vom 60s-Heartbeat oben, weil hier tatsaechlich der verknuepfte
# Riot-Account mitgeschickt wird (Puuid + Anzeigename) - das gehoert nicht in
# den haeufigen, bewusst minimalen Heartbeat. Nur fuers interne, admin-only
# Dashboard (rankyoinker.de/dashboard) gedacht, nicht oeffentlich einsehbar.
FEATURE_COUNTS_PATH = os.path.join(DATA_DIR, ".rankyoinker_feature_counts.json")
CLIENT_REPORT_URL = "https://rankyoinker.de/api/client-report"
CLIENT_REPORT_INTERVAL = 300
_LAST_REPORT_PATH = os.path.join(DATA_DIR, ".rankyoinker_last_report.json")
_feature_lock = threading.Lock()
_feature_counts = None


def _load_feature_counts():
    global _feature_counts
    if _feature_counts is None:
        data = _load_json(FEATURE_COUNTS_PATH)
        _feature_counts = data if isinstance(data, dict) else {}
    return _feature_counts


def record_feature(name):
    """Zaehlt lokal, wie oft ein Feature benutzt wurde - reiner Zaehler pro
    Name, kein Zeitpunkt, kein Zusammenhang mit einem bestimmten Match. Wird
    NICHT bei jeder Nutzung einzeln verschickt, sondern gesammelt und alle
    5 Minuten als Momentaufnahme mitgeschickt (siehe _client_report_loop)."""
    try:
        with _feature_lock:
            counts = _load_feature_counts()
            counts[name] = counts.get(name, 0) + 1
            _save_json(FEATURE_COUNTS_PATH, counts)
    except Exception:
        pass


def _riot_client_account():
    """Puuid + Riot-ID direkt aus dem lokalen Riot Client (Chat-Sitzung) -
    funktioniert, sobald man im Riot-Launcher eingeloggt ist, unabhaengig
    davon, ob VALORANT oder League laufen. None, wenn nicht ermittelbar."""
    try:
        if not os.path.exists(LOCKFILE):
            return None
        with open(LOCKFILE, "r", encoding="utf-8", errors="replace") as fh:
            parts = fh.read().strip().split(":")
        if len(parts) < 5:
            return None
        basic = base64.b64encode(("riot:" + parts[3]).encode()).decode()
        status, j = _http("https://127.0.0.1:%s/chat/v1/session" % parts[2],
                          {"Authorization": "Basic " + basic}, local_unverified=True)
        if status != 200 or not isinstance(j, dict):
            return None
        puuid = j.get("puuid")
        if not _is_puuid(puuid or ""):
            return None
        gn, tag = j.get("game_name"), j.get("game_tag")
        return {"puuid": puuid, "name": "%s#%s" % (gn, tag) if gn and tag else (gn or None)}
    except Exception:
        return None


def _current_riot_account():
    """Bestmoegliches Wissen ueber den gerade eingeloggten Riot-Account - nur
    Puuid und Anzeigename, sonst nichts. Versucht zuerst VALORANT (get_auth()),
    das aber zwingend ShooterGame.log braucht (siehe dortigen RiotError) - ein
    Account, der ausschliesslich League spielt und VALORANT nie gestartet hat,
    kam darueber nie durch und wurde im Dashboard faelschlich als "kein Account
    verknuepft" gefuehrt. Faellt deshalb seit 2026-09-29 auf den League Client
    (lol_current_summoner(), voellig unabhaengig von VALORANT/ShooterGame.log)
    zurueck, wenn der VALORANT-Weg nichts liefert. None, wenn ueber KEINEN der
    beiden Wege ein Login erkennbar ist."""
    try:
        auth = get_auth()
        puuid = auth.get("puuid")
        if puuid:
            name = None
            try:
                name = names_for([puuid]).get(puuid)
            except Exception:
                pass
            if not name:
                rc = _riot_client_account()
                if rc and rc["puuid"] == puuid:
                    name = rc["name"]
            return {"puuid": puuid, "name": name}
    except Exception:
        pass
    # Riot Client allein reicht: eingeloggt im Launcher, aber (noch) kein
    # Spiel gestartet bzw. VALORANT nie gestartet (get_auth() braucht
    # ShooterGame.log fuer die Region). Vorher landeten solche Installationen
    # im Dashboard als "kein Account".
    rc = _riot_client_account()
    if rc:
        return rc
    try:
        summoner = lol_current_summoner()
        puuid = summoner.get("puuid")
        if puuid:
            game_name = summoner.get("gameName") or summoner.get("displayName")
            tag_line = summoner.get("tagLine")
            name = "%s#%s" % (game_name, tag_line) if game_name and tag_line else game_name
            return {"puuid": puuid, "name": name}
    except Exception:
        pass
    return None


# ---- Fehler-Telemetrie fürs Admin-Dashboard ----
# Unerwartete Fehler (kaputte LCU-Verbindung, abgestürzte Automatik,
# fehlgeschlagenes Update, Ausnahmen in Endpunkten) gehen still mit dem
# Client-Report an rankyoinker.de, damit Ausfälle wie "League komplett tot
# nach 2.3.14" im Dashboard auffallen statt erst durch Zufall. Erwartete
# Zustände ("Riot Client läuft nicht") werden bewusst NICHT gemeldet.
# Nur im Speicher, je App-Start eigene Sitzungs-ID; Windows-Benutzernamen in
# Pfaden werden vor dem Senden entfernt.
TELEMETRY_MAX = 20
_telemetry_lock = threading.Lock()
_telemetry = {}
_TELEMETRY_SESSION = secrets.token_hex(6)
_USER_PATH_RE = re.compile(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/'\"\r\n]+", re.IGNORECASE)


def _scrub(text):
    return _USER_PATH_RE.sub(r"\1<user>", str(text))[:300]


def _telemetry_noise(source, err):
    """Erwartbare Netz-Aussetzer, die nichts über einen Fehler in RankYoinker
    aussagen und das Dashboard nur zumüllen (Stand 2026-10-11 die Mehrheit
    aller Einträge):
    * Browser/Handy hat die Verbindung geschlossen, bevor die Antwort raus
      war (WinError 10053/10054, Broken Pipe) - z. B. Tab gewechselt.
    * Heartbeat erreicht rankyoinker.de kurz nicht (Timeout, 5xx, DNS) -
      fehlende Heartbeats sieht man im Dashboard ohnehin.
    * League Client lehnt die Verbindung ab (WinError 10061) - passiert beim
      Schliessen/Neustarten des Clients, bevor das Lockfile weg ist."""
    if isinstance(err, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
        return True
    reason = getattr(err, "reason", None)
    if source == "heartbeat":
        if isinstance(err, urllib.error.HTTPError):
            return err.code >= 500
        return isinstance(err, (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError))
    if source == "lcu":
        return isinstance(err, ConnectionRefusedError) or isinstance(reason, ConnectionRefusedError)
    return False


def report_error(source, err):
    try:
        if _telemetry_noise(source, err):
            return
        etype = type(err).__name__ if isinstance(err, BaseException) else "Error"
        msg = _scrub(err)
        key = "%s|%s|%s" % (source, etype, msg[:120])
        now = int(time.time() * 1000)
        with _telemetry_lock:
            e = _telemetry.get(key)
            if e:
                e["count"] += 1
                e["lastAt"] = now
                return
            if len(_telemetry) >= TELEMETRY_MAX:
                del _telemetry[min(_telemetry, key=lambda k: _telemetry[k]["lastAt"])]
            _telemetry[key] = {"source": str(source)[:40], "type": etype[:60], "message": msg,
                               "count": 1, "firstAt": now, "lastAt": now}
    except Exception:
        pass


def _telemetry_snapshot():
    with _telemetry_lock:
        return sorted((dict(v) for v in _telemetry.values()), key=lambda e: -e["lastAt"])


def _client_report_loop():
    client_id = _get_client_id()
    while True:
        try:
            account = _current_riot_account()
            with _feature_lock:
                features = dict(_load_feature_counts())
            errors = _telemetry_snapshot()
            snapshot = {"account": account, "features": features, "errors": errors}
            last = _load_json(_LAST_REPORT_PATH) or {}
            sent_at = last.pop("_sentAt", 0) if isinstance(last, dict) else 0
            # Nur senden, wenn sich seit dem letzten Mal wirklich etwas
            # geaendert hat (neuer Account, neue Feature-Nutzung) - ein
            # unveraenderter Stand alle 5 Minuten erneut zu verschicken,
            # waere reine Serverlast ohne neue Information. Spaetestens alle
            # 6 h aber trotzdem: Ging der Eintrag auf dem Server verloren
            # (Aufraeumen, abgelehnter Report), haette die App sonst nie
            # wieder etwas geschickt und der Account fehlte dauerhaft.
            # Einen schon bekannten Account nicht durch "gerade kein Riot
            # Client offen" (None) ersetzen - das ist keine Aenderung.
            if account is None and isinstance(last, dict) and last.get("account"):
                account = last["account"]
                snapshot["account"] = account
            if snapshot != last or time.time() - sent_at > 6 * 3600:
                body = json.dumps({
                    "clientId": client_id,
                    "version": APP_VERSION,
                    "lang": _heartbeat_lang(),
                    "riotAccount": account,
                    "features": features,
                    "session": _TELEMETRY_SESSION,
                    "errors": errors,
                }).encode("utf-8")
                req = urllib.request.Request(
                    CLIENT_REPORT_URL,
                    data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "RankYoinker-ClientReport"},
                    method="POST",
                )
                _urlopen_public(req, timeout=10).close()
                _save_json(_LAST_REPORT_PATH, dict(snapshot, _sentAt=time.time()))
        except Exception:
            pass
        time.sleep(CLIENT_REPORT_INTERVAL)


# ---- Automatisches Update (mit Pflicht-Hash-Prüfung) ----
# Das Overlay (index.html) fragt rankyoinker.de/api/version ab und zeigt ein
# zentriertes Popup, sobald der Spieler nicht im Match ist. Klickt der Nutzer
# auf "Jetzt installieren", ruft der Browser diesen Endpunkt LOKAL auf
# (siehe _local_only()) und schickt dabei den SHA256-Hash mit, den die
# Website für die aktuelle Version angibt.
#
# SICHERHEIT: Die heruntergeladene Datei wird NUR ausgeführt, wenn ihr
# SHA256-Hash exakt mit dem von der Website gemeldeten Hash übereinstimmt.
# Ohne passenden Hash wird nichts gestartet und die Datei sofort gelöscht.
# Das verhindert, dass eine manipulierte/beschädigte Datei (kompromittierter
# Server, DNS-Hijack, Übertragungsfehler) je ausgeführt wird.
# BASE statt %TEMP% - genau wie schon bei _HEARTBEAT_ERR_PATH (das
# zuverlässig funktioniert hat). %TEMP% kann sich je nach Aufrufkontext
# (z.B. innerhalb der elevierten Installer-Kette) anders auflösen als das
# %TEMP%, das man selbst im Explorer nachschaut - die Datei existierte
# dadurch scheinbar nie, obwohl der Code lief.
_UPDATE_LOG_PATH = os.path.join(DATA_DIR, "rankyoinker-update-log.txt")


def _ulog(msg):
    """Haengt eine Zeile ans Update-Log an. Wird ab dem allerersten Schritt
    genutzt (nicht erst ab dem Installer-Start) - vorher gab es keine Spur,
    wenn schon Download oder Hash-Pruefung scheiterten, und "die Log-Datei
    fehlt komplett" liess sich nicht von "der Klick kam nie an" unterscheiden."""
    try:
        with open(_UPDATE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


VERSION_CHECK_URL = "https://rankyoinker.de/api/version"
GITHUB_RELEASE_BASE = "https://github.com/zdruqz-js/rankyoinker/releases/download"


def _resolve_official_update():
    """Ermittelt downloadUrl + Hash SELBST per eigenem, echtem HTTPS-Aufruf an
    rankyoinker.de - nimmt sie NICHT mehr vom Aufrufer entgegen (der frueher
    einfach body.get("downloadUrl")/body.get("sha256") direkt an
    trigger_self_update() durchgereicht hat). Das war die eigentliche Luecke:
    selbst mit _origin_ok_silent() als zusaetzlicher Absicherung waere es
    unsauber, dem Browser hier weiter zu vertrauen - der Hash-Vergleich
    unten ist nur noch sinnvoll, wenn beide Seiten (Soll UND Ist) wirklich
    vom Server kommen, nicht wenn der Aufrufer beide frei waehlen kann
    (Community-Sicherheitsbericht, 2026-09-28).
    Zusaetzlich: downloadUrl wird nicht aus der JSON-Antwort uebernommen,
    sondern selbst aus dem bekannten GitHub-Release-Muster gebaut (genau wie
    server.js es fuer /download tut) - selbst wenn rankyoinker.de kompromittiert
    waere und einen falschen Hash ausliefert, muesste ein Angreifer zusaetzlich
    eine passende, signierte Datei tatsaechlich unter genau dieser GitHub-
    Release-URL veroeffentlichen (siehe Sigstore-Attestation im Repo), statt
    nur irgendeine beliebige URL angeben zu koennen.
    """
    channel = "stable"
    try:
        cfg = _load_json(CONFIG_JSON_FILE)
        if str((cfg or {}).get("updateChannel", "stable")).lower() == "dev":
            channel = "dev"
    except Exception:
        pass
    url = VERSION_CHECK_URL + ("?channel=dev" if channel == "dev" else "")
    status, j = _http(url, {"User-Agent": "RankYoinker-Updater"})
    if status != 200 or not j or not j.get("version") or not j.get("sha256"):
        return None
    version = str(j["version"])
    filename = "RankYoinker-SetupV%s.exe" % version
    download_url = "%s/v%s/%s" % (GITHUB_RELEASE_BASE, version, filename)
    return download_url, str(j["sha256"]).lower(), version


def _ps_quote(value):
    """String als PowerShell-Literal in einfachen Anfuehrungszeichen."""
    return "'" + str(value).replace("'", "''") + "'"


UPDATE_PENDING_PATH = os.path.join(DATA_DIR, "rankyoinker-update-pending.json")


def _write_update_pending(version):
    """Merkt sich das Update-Ziel - der naechste Start prueft, ob es geklappt hat."""
    try:
        _save_json(UPDATE_PENDING_PATH, {"target": str(version), "from": APP_VERSION, "at": int(time.time())})
    except Exception:
        pass


def check_pending_update():
    """Beim Start: lief das letzte Update wirklich durch? Sonst als Fehler
    melden (Dashboard-Telemetrie), statt dass es lautlos verschwindet."""
    # Rest des alten Batch-Warteskripts (bis 2.4.5-dev5): der neue Installer
    # beendet dessen cmd.exe, bevor es sich selbst loeschen kann
    try:
        os.remove(os.path.join(DATA_DIR, "rankyoinker-update-wait.bat"))
    except OSError:
        pass
    try:
        if not os.path.exists(UPDATE_PENDING_PATH):
            return
        info = _load_json(UPDATE_PENDING_PATH) or {}
        os.remove(UPDATE_PENDING_PATH)
    except Exception:
        return
    target = str(info.get("target") or "")
    if not target or target == APP_VERSION:
        if target:
            _ulog("Update auf %s erfolgreich (laeuft jetzt)." % target)
        return
    if time.time() - float(info.get("at") or 0) > 6 * 3600:
        return                                # uralt, nicht mehr aussagekraeftig
    msg = "Update auf %s fehlgeschlagen, es laeuft weiter %s" % (target, APP_VERSION)
    _ulog(msg + " (siehe rankyoinker-setup.log).")
    report_error("self-update", msg)


def _update_waiter_ps1(pid, installer, target, log_path, setup_log, pythonw, script, appdir, sha256=""):
    """PowerShell-Warteskript fuer das Selbst-Update (siehe _trigger_self_update).

    Laeuft unter Windows PowerShell 5.1 - also keine PS7-Syntax (kein ?:, ??, &&).
    """
    q = _ps_quote
    return "\r\n".join([
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$log = %s" % q(log_path),
        "function L([string]$m) { Add-Content -LiteralPath $log -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.ff') + '] ' + $m) -Encoding UTF8 }",
        "function Get-Status { try { return Invoke-RestMethod -Uri 'http://127.0.0.1:1101/status' -TimeoutSec 3 -UseBasicParsing } catch { return $null } }",
        "function Test-Installer { return [bool](Get-Process -Name 'rankyoinker-update*' -ErrorAction SilentlyContinue) }",
        "$target = %s" % q(target),
        "L ('Update gestartet, warte auf PID %d...')" % int(pid),
        "try { Wait-Process -Id %d -Timeout 30 -ErrorAction Stop } catch { }" % int(pid),
        "if (Get-Process -Id %d -ErrorAction SilentlyContinue) { L 'Alter Prozess laeuft nach 30 s noch - beende ihn hart.'; Stop-Process -Id %d -Force }" % (int(pid), int(pid)),
        # Seit 2.5.3: Hash DIREKT vor dem Start noch einmal pruefen - die Datei liegt in einem
        # Ordner, den jedes Programm des Nutzers beschreiben darf, und der Installer laeuft
        # danach mit Admin-Rechten. Passt er nicht mehr: nichts starten, Dienst wieder hoch.
        "$want = %s" % q(str(sha256 or "").lower()),
        "if ($want) {",
        "  $have = ''",
        "  try { $have = (Get-FileHash -LiteralPath %s -Algorithm SHA256).Hash.ToLower() } catch { }" % q(installer),
        "  if ($have -ne $want) {",
        "    L ('ABBRUCH: Installer nach der Pruefung veraendert (' + $have + ') - nichts gestartet.')",
        "    Remove-Item -LiteralPath %s -Force -ErrorAction SilentlyContinue" % q(installer),
        "    if ((Test-Path -LiteralPath %s) -and (Test-Path -LiteralPath %s)) { Start-Process -FilePath %s -ArgumentList ('\"' + %s + '\"') -WorkingDirectory %s }" % (
            q(pythonw), q(script), q(pythonw), q(script), q(appdir)),
        "    Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue",
        "    exit",
        "  }",
        "  L 'Hash direkt vor dem Start erneut geprueft - passt.'",
        "}",
        "L ('Alter Prozess beendet, starte Installer (' + %s + ')...')" % q(installer),
        # /CLOSEAPPLICATIONS: der Restart Manager schliesst auch fremde Prozesse,
        # die Dateien in {app} offen halten (still sonst: Abbruch ohne Meldung)
        "$setupArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/CLOSEAPPLICATIONS', ('/LOG=\"' + %s + '\"'))" % q(setup_log),
        "try { Start-Process -FilePath %s -ArgumentList $setupArgs } catch { L ('Installer konnte nicht gestartet werden: ' + $_.Exception.Message) }" % q(installer),
        "$start = Get-Date",
        "$ok = $false",
        "while (((Get-Date) - $start).TotalSeconds -lt 240) {",
        "  $s = Get-Status",
        "  if ($s -and ([string]$s.appVersion -eq $target)) { $ok = $true; break }",
        "  if ((((Get-Date) - $start).TotalSeconds -gt 20) -and -not (Test-Installer)) {",
        "    Start-Sleep -Seconds 10",
        "    $s = Get-Status",
        "    if ($s -and ([string]$s.appVersion -eq $target)) { $ok = $true }",
        "    break",
        "  }",
        "  Start-Sleep -Seconds 1",
        "}",
        "if ($ok) {",
        "  L ('Update erfolgreich: Version ' + $target + ' laeuft.')",
        "} else {",
        "  $s = Get-Status",
        "  if ($s) {",
        "    L ('UPDATE FEHLGESCHLAGEN: es laeuft weiter Version ' + [string]$s.appVersion + ' statt ' + $target + '. Installer-Log: ' + %s)" % q(setup_log),
        "  } elseif (Test-Installer) {",
        "    L 'Installer laeuft nach 240 s noch - kein Sicherheitsnetz, um ihn nicht zu stoeren.'",
        "  } else {",
        "    L 'Log-Server laeuft nicht und kein Installer mehr aktiv - starte Sicherheitsnetz direkt...'",
        "    if ((Test-Path -LiteralPath %s) -and (Test-Path -LiteralPath %s)) { Start-Process -FilePath %s -ArgumentList ('\"' + %s + '\"') -WorkingDirectory %s }" % (
            q(pythonw), q(script), q(pythonw), q(script), q(appdir)),
        "  }",
        "}",
        "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue",
        "",
    ])


def trigger_self_update():
    res = _trigger_self_update()
    if isinstance(res, dict) and not res.get("ok"):
        report_error("self-update", res.get("error") or "unbekannt")
    return res


def _trigger_self_update():
    resolved = _resolve_official_update()
    if not resolved:
        _ulog("Abgebrochen: konnte Versionsinfo nicht von rankyoinker.de laden.")
        return {"ok": False, "error": "Konnte aktuelle Version nicht ermitteln."}
    download_url, expected_sha256, version = resolved
    _ulog(f"Update ausgeloest. version={version!r} downloadUrl={download_url!r} sha256={expected_sha256!r}")

    tmp_path = os.path.join(DATA_DIR, "rankyoinker-update.exe")

    try:
        req = urllib.request.Request(download_url, headers={"User-Agent": "RankYoinker-Updater"})
        with _urlopen_public(req, timeout=60) as resp, open(tmp_path, "wb") as out:
            out.write(resp.read())
        _ulog(f"Download erfolgreich -> {tmp_path}")
    except Exception as e:
        _ulog(f"Download fehlgeschlagen: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"Download fehlgeschlagen: {e}"}

    # --- Pflicht-Integritätsprüfung, bevor irgendetwas ausgeführt wird ---
    digest = hashlib.sha256()
    try:
        with open(tmp_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    except Exception as e:
        _ulog(f"Datei konnte nicht geprueft werden: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"Datei konnte nicht geprueft werden: {e}"}

    actual = digest.hexdigest().lower()
    if not hmac.compare_digest(actual, str(expected_sha256).lower()):
        _ulog(f"Hash-Mismatch: erwartet={expected_sha256} tatsaechlich={actual}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return {"ok": False, "error": "Hash stimmt nicht ueberein - Update abgebrochen, nichts wurde ausgefuehrt."}
    # --- Ab hier ist die Datei verifiziert ---
    _ulog("Hash passt, verifiziert.")

    _kill_all_vry()  # vry.exe blockiert sonst das Ueberschreiben seiner eigenen Datei
    _ulog("_kill_all_vry() aufgerufen.")
    _kill_other_log_servers()  # Ueberbleibsel-Instanzen aus frueheren Sitzungen mit erledigen
    _ulog("_kill_other_log_servers() aufgerufen.")

    # WICHTIG: dieser Python-Prozess selbst hält python310.dll aus {app}
    # offen (er läuft ja gerade daraus). Den Installer einfach sofort zu
    # starten und "kurz danach" (per festem Sleep) hart zu beenden war ein
    # Wettlauf: /VERYSILENT kopiert seine Dateien so schnell wie möglich
    # und erreichte python310.dll oft schon, WAEHREND der alte Prozess sie
    # noch offen hatte - die Kopie scheiterte dann lautlos (SUPPRESSMSGBOXES
    # unterdrückt genau die Meldung, die das gezeigt hätte), das Update
    # sah aus wie "läuft", passierte aber nie richtig.
    #
    # Fix: ein winziges Batch-Skript wartet aktiv (per tasklist-Polling), bis
    # DIESE PID wirklich weg ist, und startet den Installer erst danach.
    my_pid = os.getpid()
    # Protokolliert die Schritte NACH diesem Python-Prozess (der sich ja
    # gleich selbst beendet und darum ab hier nichts mehr sehen kann) -
    # insbesondere den Exit-Code des Installers. Ohne das war "es sieht aus
    # als würde nur der Dienst neu starten, aber nichts installiert sich"
    # nicht nachvollziehbar: Windows zeigt bei einem admin-pflichtigen
    # Installer IMMER eine UAC-Bestätigung (auch bei /VERYSILENT - das
    # unterdrückt nur die Installer-eigene Oberfläche, nicht den
    # UAC-Dialog von Windows selbst) - wird die übersehen/abgelehnt/verpasst
    # das Zeitlimit, installiert sich nichts, obwohl install_all.py über
    # den ALTEN Autostart-Eintrag den Log-Server trotzdem wieder hochbringt.
    log_path = _UPDATE_LOG_PATH  # dasselbe Log wie oben - eine durchgehende Spur
    # WICHTIG (aus einem echten Hänger gelernt): "find" mit der reinen PID
    # als Text kann mit ANDEREN Zahlen in der tasklist-Ausgabe kollidieren
    # (Arbeitsspeicher-Wert, Sitzungsnummer, eine andere PID, ...) - dann
    # denkt die Schleife für immer, der alte Prozess liefe noch, obwohl er
    # längst weg ist. "pythonw.exe" als Suchtext kann mit sowas nicht
    # kollidieren. Zusätzlich zur Sicherheit auf 30 Versuche (~30s) gedeckelt,
    # damit das Skript nie wieder unbegrenzt hängen bleiben kann - notfalls
    # startet der Installer trotzdem und der Dateikonflikt wird sichtbar,
    # statt lautlos ewig zu warten.
    # WICHTIG (aus einem echten Vorfall gelernt): "start /wait" auf einen
    # admin-pflichtigen Installer haengt UNBEGRENZT, wenn die UAC-Abfrage
    # (Benutzerkontensteuerung) nie beantwortet wird, z. B. weil niemand am
    # PC sitzt. Bis dahin war der alte Prozess schon beendet, der neue kam
    # aber nie hoch - das Tool blieb bis zum naechsten Neustart komplett
    # tot. Fix: kein /wait mehr, stattdessen selbst mit Zeitlimit pollen.
    #
    # WICHTIG (aus einem zweiten echten Vorfall gelernt): Ein erster Versuch
    # hat hier per "tasklist /fi IMAGENAME eq rankyoinker-update.exe"
    # gewartet, bis der Installer-PROZESSNAME verschwindet. Weil der
    # Installer erhoeht laufen muss (PrivilegesRequired=admin), startet
    # Inno Setup dafuer aber intern eine ELEVIERTE KOPIE unter einem
    # ANDEREN Namen/Pfad (ein is-XXXXX.tmp-Ordner) und der urspruenglich
    # gestartete "rankyoinker-update.exe"-Prozess beendet sich dabei fast
    # sofort selbst, nachdem er das ausgeloest hat. Die Schleife dachte
    # darum "fertig" nach unter einer Sekunde, WEIT bevor die eigentliche
    # (erhoehte) Installation inkl. install_all.py ueberhaupt fertig war -
    # das Sicherheitsnetz unten griff dadurch praktisch immer sofort,
    # statt install_all.py eine echte Chance zu geben.
    #
    # Fix: statt auf einen Prozessnamen zu warten, direkt auf das Ergebnis
    # warten, das wirklich zaehlt - antwortet /status wieder? Das gibt dem
    # echten Installationsweg (inkl. install_all.py) bis zu ~2 Minuten
    # Zeit, bevor das Sicherheitsnetz unten ueberhaupt in Erwaegung
    # gezogen wird.
    #
    # WICHTIG (dritter Vorfall, 2026-10-13, Live-Analyse der Session "Ladezeit
    # der letzten 5 Matches"): Das Batch-Warteskript bis 2.4.5-dev5 hat NIE
    # gewartet. In "tasklist ... 2^>nul ^| find" innerhalb eines ( )-Blocks
    # machen die ^ Umleitung und Pipe zu Text - tasklist bekam ungueltige
    # Argumente, die Schleife brach nach 20 ms ab. Der Installer startete,
    # waehrend der alte Dienst noch lief; dessen /status beantwortete die
    # Fertig-Pruefung, und ~3 s spaeter startete das Sicherheitsnetz den ALTEN
    # Dienst mitten in die Installation. Lief der Dienst erhoeht (kein UAC),
    # brach der unsichtbare Installer dann lautlos ab ("Datei in Benutzung").
    #
    # Jetzt ein PowerShell-Skript statt Batch:
    #   1. Wait-Process auf die eigene PID (kein Text-Matching mehr),
    #   2. Installer mit /LOG starten (Abbrueche nachvollziehbar),
    #   3. fertig erst, wenn /status die NEUE appVersion meldet,
    #   4. Sicherheitsnetz nur, wenn KEIN Installer mehr laeuft und gar kein
    #      Dienst antwortet - nie waehrend einer laufenden Installation.
    # Der neue Installer raeumt zusaetzlich selbst auf (RankYoinker.iss:
    # altes Warteskript beenden, laufende Dienste vor dem Kopieren stoppen) -
    # das schuetzt auch Updates, die noch vom alten Batch-Skript kommen.
    pythonw_path = os.path.join(BASE, "pythonw.exe")
    script_path = os.path.join(BASE, "vry_log_server.py")
    setup_log = os.path.join(DATA_DIR, "rankyoinker-setup.log")
    waiter_path = os.path.join(DATA_DIR, "rankyoinker-update-wait.ps1")
    _write_update_pending(version)
    waiter_script = _update_waiter_ps1(my_pid, tmp_path, version, log_path, setup_log,
                                       pythonw_path, script_path, BASE, sha256=actual)

    try:
        # utf-8-sig: Windows PowerShell 5.1 liest .ps1 ohne BOM als ANSI -
        # Pfade mit z. B. koreanischem Benutzernamen gingen sonst kaputt
        with open(waiter_path, "w", encoding="utf-8-sig") as f:
            f.write(waiter_script)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", waiter_path],
            creationflags=CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200),
            close_fds=True,
        )
        _ulog(f"Warte-Skript gestartet ({waiter_path}), beende mich selbst gleich.")
    except Exception as e:
        _ulog(f"Warte-Skript/Installer konnte nicht gestartet werden: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"Installer konnte nicht gestartet werden: {e}"}

    # Kurz warten, damit diese HTTP-Antwort noch rausgeht, dann hart beenden -
    # das Batch-Skript oben wartet ab hier auf das wirkliche Prozessende und
    # startet den Installer danach. Der startet die App automatisch neu
    # (siehe RankYoinker.iss).
    def _delayed_exit():
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"ok": True}
# ===================================================================================


def _watchdog():
    """Haelt genau eine vry.exe am Leben, solange VALORANT laeuft.

    * VALORANT an/aus    -> vry.exe folgt automatisch (siehe _follow_valorant)
    * mehrere Instanzen  -> ueberzaehlige beenden
    * abgestuerzt        -> Reste sicher wegraeumen und neu starten
    * VALORANT fehlt     -> warten statt starten, Start bleibt vorgemerkt
    """
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            _follow_valorant()
            with _proc_lock:
                if not _proc["desired"] or _proc["giveup"]:
                    _proc["misses"] = 0
                    # Von aussen gestartet (Verknüpfung, alte Sitzung)? Dann
                    # übernehmen wir die Aufsicht — aber nur solange VALORANT
                    # läuft und nie direkt nach einem bewussten Stopp.
                    if (not _proc["giveup"] and not _proc["desired"]
                            and time.time() - _proc["last_stop"] > 10
                            and valorant_present() and vry_running()):
                        _proc["desired"] = True
                    continue
                # Startbefehl noch unterwegs -> Prozess darf fehlen
                if time.time() - _proc["last_start"] < START_GRACE:
                    continue
                pids = vry_pids()
                if pids:
                    _proc["misses"] = 0
                    _proc["armed"] = False
                    if _proc.get("pid") not in pids and len(pids) == 1:
                        _proc["pid"] = pids[0]
                    _enforce_single(pids)
                    continue

                # ---- ab hier läuft keine vry.exe ----

                # Ohne VALORANT bringt ein Start nichts: vry.exe landet nur in
                # der Retry-Schleife. Und solange die Region fehlt, würde es
                # sich beim Start aufhängen. Also vormerken und warten.
                if not vry_ready():
                    _proc["misses"] = 0
                    if not _proc["armed"]:
                        _proc["armed"] = True
                        _proc["note"] = ("Warte auf VALORANT — RankYoinker startet "
                                         "automatisch, sobald das Spiel laeuft."
                                         if not game_ready() else
                                         "VALORANT startet noch — RankYoinker wartet auf die "
                                         "Region im Spiel-Log.")
                    continue

                # VALORANT ist jetzt da und ein Start war vorgemerkt -> nachholen.
                # Das zählt bewusst NICHT als Absturz-Neustart.
                if _proc["armed"]:
                    _proc["armed"] = False
                    _proc["misses"] = 0
                    res = start_vry(force=True)
                    _proc["note"] = ("VALORANT erkannt — RankYoinker wurde automatisch gestartet."
                                     if res.get("ok")
                                     else "Start fehlgeschlagen: %s"
                                          % res.get("error", "unbekannt"))
                    continue

                # Prozess fehlt — erst beim zweiten Mal glauben (tasklist zickt
                # gelegentlich während des Startens).
                _proc["misses"] += 1
                if _proc["misses"] < 2:
                    continue

                now = time.time()
                if now - _proc["last_restart"] > RESTART_WINDOW:
                    _proc["restarts"] = 0
                if _proc["restarts"] >= MAX_RESTARTS:
                    _proc["giveup"] = True
                    _proc["desired"] = False
                    _proc["note"] = ("RankYoinker ist %d Mal hintereinander abgestuerzt — "
                                     "automatischer Neustart pausiert." % MAX_RESTARTS)
                    continue

                # Sicherstellen, dass wirklich nichts Altes mehr hängt
                _kill_all_vry()
                _proc["restarts"] += 1
                _proc["last_restart"] = now
                _proc["misses"] = 0
                res = start_vry(force=True)
                _proc["note"] = ("RankYoinker war beendet — automatisch neu gestartet "
                                 "(%d/%d)." % (_proc["restarts"], MAX_RESTARTS)) \
                    if res.get("ok") else ("Neustart fehlgeschlagen: %s"
                                           % res.get("error", "unbekannt"))
        except Exception:
            pass


def proc_state():
    """Prozess-Infos fuer die Oberflaeche (Start/Stop-Buttons, Absturz-Hinweis)."""
    # Bewusst vor dem Lock: das sind tasklist-Aufrufe, die soll der Watchdog
    # nicht ausbremsen.
    running = vry_running()
    valorant = valorant_present()
    ready = game_ready()
    with _proc_lock:
        return {
            "running": running,
            "desired": bool(_proc["desired"]),
            "restarts": _proc["restarts"],
            "lastRestart": int(_proc["last_restart"]),
            "note": _proc["note"],
            "giveup": bool(_proc["giveup"]),
            "auto": bool(_proc["auto"]),
            "valorant": valorant,
            "gameReady": ready,
            # vRY läuft nicht, kommt aber von selbst, sobald VALORANT da ist
            "waiting": bool(not running and not _proc["giveup"]
                            and (_proc["armed"] or _proc["auto"])),
        }


# ============================ Riot-Auth + Requests ============================

class RiotError(Exception):
    pass


_auth_lock = threading.Lock()
_auth = {"ts": 0.0, "data": None}


def _unzip(raw, headers):
    """Riot-Antworten kommen mit "Accept-Encoding: gzip" gepackt (Match-Details
    ~40 statt ~420 KB - weniger Datenverkehr neben dem laufenden Spiel)."""
    if raw and headers is not None and (headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            return raw
    return raw


def _http(url, headers=None, method="GET", body=None, timeout=12, local_unverified=False, info=None):
    """local_unverified=True NUR fuer Riot-APIs auf 127.0.0.1 mit
    selbstsigniertem Zertifikat (Verifikation dort schlicht nicht moeglich):
    die Riot-Client-API (get_auth()/_peek_subject()), die League-Client-API
    (lcu_req()) und die League Live Client Data API auf Port 2999
    (_live_get()). Die beiden League-Aufrufer hatten die Flag beim
    Sicherheitsfix vom 2026-09-28 zunaechst nicht bekommen - dadurch schlug
    JEDER LoL-Endpunkt mit CERTIFICATE_VERIFY_FAILED fehl (behoben
    2026-10-04). Jeder andere Aufruf (valorant-api.com, pd./glz.*.a.pvp.net
    ueber riot_get/riot_post/riot_put, rankyoinker.de) verifiziert das
    Zertifikat normal - frueher liefen ALLE Anfragen ueber denselben
    unverifizierten Kontext (_SSL), was echte Man-in-the-Middle-Angriffe auf
    JEDE Verbindung erlaubt haette, nicht nur auf die lokale (siehe
    Community-Sicherheitsbericht 2026-09-28). Gleiches Certifi-Fallback-Muster
    wie _urlopen_public(): Standard-Systemkontext zuerst, bei einem echten
    TLS-Fehler einmal mit dem mitgelieferten certifi-Bundle erneut versuchen
    (bestimmte eingebettete Python-Distributionen auf Windows finden das
    Systemdepot nicht zuverlaessig) - beides verifiziert weiterhin voll,
    nur die CA-Quelle unterscheidet sich.
    """
    # info (optional, dict): bekommt bei Fehlerantworten "raw" (Anfang des
    # Rohtexts) und "retry_after" - damit riot_get() eine Cloudflare-Sperre
    # (HTML statt JSON) von einem normalen Riot-Rate-Limit unterscheiden kann.
    req = urllib.request.Request(url, method=method,
                                 data=(body.encode("utf-8") if body else None))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    ctx = _SSL if local_unverified else None
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                raw = _unzip(r.read(), r.headers)
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.URLError as e:
            if (not local_unverified) and _CERTIFI_CTX and isinstance(e.reason, ssl.SSLError):
                with urllib.request.urlopen(req, timeout=timeout, context=_CERTIFI_CTX) as r:
                    raw = _unzip(r.read(), r.headers)
                    return r.status, (json.loads(raw) if raw else None)
            raise
    except urllib.error.HTTPError as e:
        raw = _unzip(e.read(), e.headers)
        if info is not None:
            info["raw"] = (raw or b"")[:600]
            info["retry_after"] = e.headers.get("Retry-After") if e.headers else None
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, None


RE_GLZ = re.compile(r"https://glz-([a-z0-9-]+)\.([a-z0-9]+)\.a\.pvp\.net")
RE_VERSION = re.compile(r"CI server version: (\S+)")


def _read_shooter_log():
    try:
        with open(SHOOTER_LOG, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def _client_version(shooter_text=None):
    """Client-Version MUSS zur installierten Version passen, sonst antwortet
    Riot mit 403. valorant-api.com hinkt oft hinterher -> erst das Spiel-Log."""
    text = _read_shooter_log() if shooter_text is None else shooter_text
    hits = RE_VERSION.findall(text or "")
    if hits:
        return hits[-1]
    try:
        _, j = _http("https://valorant-api.com/v1/version")
        return (j or {}).get("data", {}).get("riotClientVersion")
    except Exception:
        return None


def _region_from_logs(shooter_text=None):
    """Region/Shard so ermitteln, wie vry.exe es auch macht: aus ShooterGame.log."""
    text = _read_shooter_log() if shooter_text is None else shooter_text
    hits = RE_GLZ.findall(text or "")
    if hits:
        pod, shard = hits[-1]
        return pod, shard
    return None, None


def _major(version):
    """'release-13.02-shipping-15-5253245' -> '13' (fuer den User-Agent)."""
    m = re.search(r"release-(\d+)", version or "")
    return m.group(1) if m else "13"


def get_auth(force=False):
    """Token + Hosts aus dem lokalen Riot Client. Ergebnis wird kurz gecacht."""
    with _auth_lock:
        if not force and _auth["data"] and time.time() - _auth["ts"] < 240:
            return _auth["data"]

        if not os.path.exists(LOCKFILE):
            raise RiotError("Riot Client laeuft nicht (kein Lockfile gefunden).")
        with open(LOCKFILE, "r", encoding="utf-8", errors="replace") as fh:
            parts = fh.read().strip().split(":")
        if len(parts) < 5:
            raise RiotError("Lockfile unlesbar.")
        local_port, local_pw = parts[2], parts[3]

        basic = base64.b64encode(("riot:" + local_pw).encode()).decode()
        status, ent = _http("https://127.0.0.1:%s/entitlements/v1/token" % local_port,
                            {"Authorization": "Basic " + basic}, local_unverified=True)
        if status != 200 or not ent or not ent.get("accessToken"):
            raise RiotError("Konnte keine Riot-Token holen (Status %s)." % status)

        shooter = _read_shooter_log()          # nur einmal lesen (Datei ist gross)
        pod, shard = _region_from_logs(shooter)
        if not shard:
            raise RiotError("Region nicht ermittelbar — VALORANT einmal gestartet haben.")
        version = _client_version(shooter)

        data = {
            "puuid": ent.get("subject"),
            "shard": shard,
            "pod": pod,
            "version": version,
            "pd": "https://pd.%s.a.pvp.net" % shard,
            "glz": "https://glz-%s.%s.a.pvp.net" % (pod, shard),
            "headers": {
                "Authorization": "Bearer " + ent["accessToken"],
                "X-Riot-Entitlements-JWT": ent.get("token", ""),
                "X-Riot-ClientPlatform": CLIENT_PLATFORM,
                "X-Riot-ClientVersion": version or "",
                # Ohne passenden User-Agent antwortet Cloudflare mit "error code: 1010"
                # (Python-urllib wird geblockt). Gleiche Kennung wie der Spiel-Client.
                "User-Agent": "ShooterGame/%s Windows/10.0.19043.1.256.64bit" % _major(version),
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",           # siehe _unzip()
            },
        }
        _auth["data"] = data
        _auth["ts"] = time.time()
    note_account(data["puuid"])
    return data


# ---- Bremse fuer pd.*.a.pvp.net ----
# Beim Matchstart fragten vry.exe, die Spielerkarten (letzte 5 Matches), die
# Party-Erkennung und "meistgespielter Agent" denselben Riot-Server parallel ab
# - weit ueber 100 Anfragen in kurzer Zeit. Riot antwortete mit 429, danach
# sperrte Cloudflare die IP kurzzeitig ganz ("Access denied | pd.eu.a.pvp.net
# used Cloudflare to restrict access", Nutzer-Log 2026-10-13). Jedes weitere
# Anklopfen verlaengert so eine Sperre nur. Darum laufen alle pd-Anfragen
# hier durch: mit Mindestabstand (wird nach einem 429 groesser und erholt sich
# langsam wieder) und mit einer echten Pause, solange eine Sperre gilt - in der
# Zeit kommt sofort 429 zurueck, ohne Riot zu fragen.
#
# Seit 2.4.9 GEMEINSAM mit vry.exe (src/requestsV.py, gleiche Logik, gleiche
# Datei): Bis dahin hatte jeder Prozess seine eigene Bremse, zusammen gingen
# beim Matchstart ueber 7 Anfragen/s raus, und waehrend der eine pausierte,
# fragte der andere weiter (Telemetrie 2026-10-14: alle gemeldeten Sperren
# waren Cloudflare, eine Installation 38-mal). Takt, Pause und Sperrzaehler
# liegen jetzt in DATA_DIR/pd-gate.json, geschuetzt durch eine Sperrdatei
# (pd-gate.lock, per O_EXCL angelegt - unter Windows und Linux gleich). Klappt
# das einmal nicht, bremst jeder Prozess wieder fuer sich (wie vor 2.4.9).
# Pause nach einer Cloudflare-Sperre erst 20 s, dann EINE Testanfrage; ist die
# Sperre noch da, 45 s, dann 90 s. 10 Minuten ohne Sperre setzen das zurueck.
PD_MIN_INTERVAL = 0.3          # Grundabstand: hoechstens ~3 Anfragen pro Sekunde, beide Prozesse zusammen
PD_MAX_INTERVAL = 1.5
PD_RATE_PAUSE = 12.0           # Pause nach einem normalen Riot-429
PD_BLOCK_PAUSES = (20.0, 45.0, 90.0)   # nach der 1./2./3. Cloudflare-Sperre in Folge
PD_STRIKE_RESET = 600.0        # so lange ohne Sperre -> wieder bei 20 s anfangen
PD_PROBE_TIMEOUT = 15.0        # Testanfrage ohne Antwort gilt danach als verloren
PD_GATE_FILE = os.path.join(DATA_DIR, "pd-gate.json")
PD_GATE_LOCKFILE = os.path.join(DATA_DIR, "pd-gate.lock")
# hi_until (seit 2.5.0-dev17): bis wann vry.exe gerade Namen/Raenge laedt - so
# lange halten sich die "Letzte 5"-Abfragen des Dienstes zurueck (_pd_bulk_wait)
PD_HI_HOLD = 3.0
_pd_gate = {"next": 0.0, "interval": PD_MIN_INTERVAL, "blocked_until": 0.0, "reason": "",
            "strikes": 0, "last_block": 0.0, "need_probe": False, "probe_until": 0.0, "hi_until": 0.0,
            # Kontext fuer Sperren-Meldungen (seit 2.5.2, beide Prozesse gleich halten): pd-Anfragen
            # der letzten 60 s je Prozess und die letzte Sperre, die vry.exe ausgeloest hat
            # (vry.exe schickt selbst keine Fehlerberichte - der Dienst meldet sie mit)
            "svc60": 0, "svc60_at": 0.0, "vry60": 0, "vry60_at": 0.0, "vry_blk_at": 0.0, "vry_blk_msg": ""}
_pd_gate_lock = threading.Lock()


_pd_shared_off = {"until": 0.0}


def _pd_file_lock():
    # Liess sich die Sperrdatei zuletzt gar nicht bekommen (z. B. haelt ein
    # Virenscanner sie fest), nicht jede Anfrage erneut 1 s warten lassen.
    if time.time() < _pd_shared_off["until"]:
        return False
    deadline = time.time() + 1.0
    while True:
        try:
            os.close(os.open(PD_GATE_LOCKFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(PD_GATE_LOCKFILE) > 3.0:
                    os.remove(PD_GATE_LOCKFILE)   # Halter ist abgestuerzt
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                _pd_shared_off["until"] = time.time() + 30.0
                return False
            time.sleep(0.003)
        except OSError:
            return False


class _PdState:
    """with _PdState() as g: gemeinsamer Zustand beider Prozesse, wird beim Verlassen gespeichert."""

    def __enter__(self):
        _pd_gate_lock.acquire()
        self.shared = _pd_file_lock()
        if self.shared:
            try:
                with open(PD_GATE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in (data.items() if isinstance(data, dict) else ()):
                    if k in _pd_gate and isinstance(v, (int, float, str)):
                        _pd_gate[k] = v
            except (OSError, ValueError):
                pass
        return _pd_gate

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.shared:
                if exc_type is None:
                    try:
                        with open(PD_GATE_FILE, "w", encoding="utf-8") as f:
                            json.dump(_pd_gate, f)
                    except OSError:
                        pass
                try:
                    os.remove(PD_GATE_LOCKFILE)
                except OSError:
                    pass
        finally:
            _pd_gate_lock.release()
        return False


def _pd_acquire():
    """Wartet auf den naechsten freien Platz. False = gerade gesperrt, nicht
    anfragen; "probe" = die eine Testanfrage nach einer Pause; sonst True."""
    slot, waited = None, 0.0
    while True:
        with _PdState() as g:
            now = time.time()
            if now < g["blocked_until"]:
                return False                    # auch wenn die Sperre waehrend des Wartens kam
            if now < g["probe_until"]:
                pass                            # Testanfrage laeuft gerade - kurz abwarten
            elif g["need_probe"]:
                g["need_probe"] = False
                g["probe_until"] = now + PD_PROBE_TIMEOUT
                g["next"] = max(g["next"], now + g["interval"])
                return "probe"
            elif slot is not None and slot <= now:
                return True
            elif slot is None:
                slot = max(now, g["next"])
                g["next"] = slot + g["interval"]
                if slot <= now:
                    return True                 # Platz ist sofort frei
        if slot is None or time.time() >= slot:
            if waited >= 5.0:
                return False
            time.sleep(0.2)
            waited += 0.2
        else:
            time.sleep(max(0.0, slot - time.time()))


def _is_cloudflare_block(info):
    raw = (info or {}).get("raw") or b""
    low = raw.lower()
    # "error code: 1015" ist Cloudflares Ratenlimit - Klartext, kein HTML. Bis
    # 2026-10-13 galt es als normales 429: 12 s Pause, dann wieder anklopfen,
    # waehrend die Sperre noch lief (beim Matchstart im vry-Log gesehen).
    return (b"cloudflare" in low or b"access denied" in low or b"<html" in low
            or b"error code:" in low)


# ---- Kontext fuer Sperren-Meldungen (2.5.2) ----
# Live 2026-10-16: Sperren fast nur bei koreanischen Installationen, teils mit 9-10
# Riot-Accounts (PC-Bang mit geteilter IP?). Die Meldung allein sagte nicht, wann und
# bei welcher Last - jetzt mit Spielphase, Region und Anfragen der letzten 60 s.
PD_CTX_WINDOW = 60.0
_pd_recent = collections.deque()           # (Zeitpunkt, Art) der eigenen pd-Anfragen
_pd_recent_lock = threading.Lock()
_game_hist = collections.deque(maxlen=12)  # (seit, Zustand) - fuer "PREGAME seit 12 s"
_vry_blk_seen = {"at": time.time()}        # Sperren von vry.exe vor dem Start nicht nachmelden


def _pd_recent_note(kind, now):
    with _pd_recent_lock:
        _pd_recent.append((now, kind or "other"))
        while _pd_recent and now - _pd_recent[0][0] > PD_CTX_WINDOW:
            _pd_recent.popleft()
        return len(_pd_recent)


def _pd_recent_kinds(t):
    """Eigene pd-Anfragen der 60 s vor t, je Art."""
    with _pd_recent_lock:
        return collections.Counter(k for ts, k in _pd_recent if t - PD_CTX_WINDOW <= ts <= t + 0.5)


def _kinds_txt(c):
    return ", ".join("%s %d" % (k, n) for k, n in c.most_common(5))


def _state_at(t):
    state, since = None, 0.0
    for ts, st in list(_game_hist):
        if ts <= t:
            state, since = st, ts
    return (state or _game.get("state") or "?"), (int(t - since) if since else None)


def _pd_block_context(t, kind, own_label, own_kinds, other_label, other_n):
    state, secs = _state_at(t)
    shard = ((_auth.get("data") or {}).get("shard")) or "?"
    own_n = sum(own_kinds.values())
    parts = ["%s%s" % (state, " seit %d s" % secs if secs is not None else ""), shard]
    if kind:
        parts.append("bei %s" % kind)
    parts.append("60 s: %s %d%s, %s %d" % (own_label, own_n, " (%s)" % _kinds_txt(own_kinds) if own_n else "",
                                          other_label, other_n))
    return " | " + " | ".join(parts)


def _pd_vry_blocks_tick():
    """Sperre, die vry.exe ausgeloest hat (steht in pd-gate.json), als Fehler mitmelden."""
    g = _pd_gate_read()
    at = float(g.get("vry_blk_at") or 0)
    if at <= _vry_blk_seen["at"]:
        return
    _vry_blk_seen["at"] = at
    try:
        d = json.loads(g.get("vry_blk_msg") or "{}")
    except ValueError:
        d = {}
    kinds = collections.Counter({str(k)[:12]: int(v) for k, v in (d.get("kinds") or {}).items() if isinstance(v, int)})
    msg = "vry.exe: Cloudflare-Sperre auf pd (Status %s) - %ss Pause (%s. in Folge)" % (
        d.get("st", "?"), d.get("p", "?"), d.get("k", "?"))
    report_error("riot-ratelimit", msg + _pd_block_context(at, str(d.get("ep") or "")[:12], "vry.exe", kinds,
                                                           "Dienst", sum(_pd_recent_kinds(at).values())))


def _pd_feedback(status, info, probe=False):
    """Ergebnis einer pd-Anfrage zurueckmelden: Sperre setzen bzw. Tempo anpassen."""
    blocked_msg = None
    kind = (info or {}).get("kind")
    n60 = _pd_recent_note(kind, time.time())
    with _PdState() as g:
        now = time.time()
        g["svc60"], g["svc60_at"] = n60, now
        vry_n = int(g.get("vry60") or 0) if now - float(g.get("vry60_at") or 0) <= PD_CTX_WINDOW else 0
        if probe:
            g["probe_until"] = 0.0
        if status in (403, 429) and _is_cloudflare_block(info):
            # Mehrere gleichzeitig laufende Anfragen melden dieselbe Sperre -
            # nur die erste (bzw. die Testanfrage danach) zaehlt.
            if now >= g["blocked_until"]:
                strikes = g["strikes"] + 1 if now - g["last_block"] < PD_STRIKE_RESET else 1
                pause = PD_BLOCK_PAUSES[min(strikes, len(PD_BLOCK_PAUSES)) - 1]
                g.update(strikes=strikes, last_block=now, blocked_until=now + pause,
                         need_probe=True, interval=PD_MAX_INTERVAL, reason="cloudflare")
                blocked_msg = "Cloudflare-Sperre auf pd (Status %s) - %ds Pause (%d. in Folge)" % (status, pause, strikes)
        elif status == 429:
            try:
                wait = float((info or {}).get("retry_after") or 0)
            except (TypeError, ValueError):
                wait = 0
            g["blocked_until"] = max(g["blocked_until"], now + max(PD_RATE_PAUSE, min(wait, 120)))
            g["interval"] = min(PD_MAX_INTERVAL, g["interval"] * 1.6)
            g["reason"] = "ratelimit"
            if probe:
                g["need_probe"] = True
        elif status and status < 400:
            # langsam wieder schneller werden
            g["interval"] = max(PD_MIN_INTERVAL, g["interval"] * 0.97)
            if probe:
                # Diese Pause hat gereicht: die naechste Sperre faengt wieder bei ihr an
                g["strikes"] = max(0, g["strikes"] - 1)
        elif probe:
            g["need_probe"] = True              # Testanfrage ohne verwertbare Antwort -> naechste testet
    if blocked_msg:
        report_error("riot-ratelimit", blocked_msg + _pd_block_context(now, kind, "Dienst", _pd_recent_kinds(now),
                                                                       "vry.exe", vry_n))


def _pd_gate_read():
    """Gemeinsamer Stand ohne Sperrdatei (nur lesen)."""
    g = _pd_gate
    try:
        with open(PD_GATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            g = dict(_pd_gate, **{k: v for k, v in data.items() if k in _pd_gate})
    except (OSError, ValueError):
        pass
    return g


def pd_gate_state():
    """Fuer die Oberflaeche: laeuft gerade eine Riot-Pause, und warum? Liest den
    gemeinsamen Stand, zeigt also auch eine Pause, die vry.exe ausgeloest hat."""
    g = _pd_gate_read()
    left = g["blocked_until"] - time.time()
    return {"paused": left > 0, "seconds": max(0, int(left + 0.5)), "reason": g["reason"] if left > 0 else ""}


# Vorrang innerhalb des Dienstes (2.4.9): Die Party-Erkennung braucht beim
# Matchstart viele pd-Anfragen und lief bisher gleichberechtigt neben den
# Kacheln "Letzte 5" - beide wurden dadurch spaet fertig. Jetzt warten ihre
# Anfragen (markiert per _low_priority, eigener Pool), solange Kachel- und
# andere Abfragen anstehen, hoechstens PD_LOW_MAX_WAIT.
PD_LOW_MAX_WAIT = 25.0
PD_LOW_HI_MAX_WAIT = 8.0                # so lange hoechstens auf vry.exe (hi_until)
# Seit 2.5.0-dev17 eine dritte, unterste Stufe: "Letzte 5" (Kacheln, meistgespielter
# Agent; markiert per _bulk_priority). Live 2026-10-15: 49 von 62 pd-Anfragen eines
# Matches waren Letzte 5, die Cloudflare-Sperre kam mitten darin, und vry.exe wartete
# danach 27 s auf die Namen. Nutzerwunsch: Letzte 5 ganz zuletzt. Sie warten jetzt,
# solange Namen/Raenge (auch aus vry.exe, siehe hi_until) oder die Party-Erkennung
# anstehen, gehen hoechstens alle PD_BULK_INTERVAL raus und pausieren nach einer
# Sperre ab deren Beginn PD_BULK_AFTER_BLOCK - die Grunddaten fragen als Erste wieder.
PD_BULK_INTERVAL = 1.2
PD_BULK_AFTER_BLOCK = 90.0
PD_BULK_MAX_WAIT = 45.0
_tl = threading.local()
_pd_high = {"n": 0}
_pd_low = {"n": 0}                      # Party-Erkennung: wartend oder laufend
# Party-Erkennung insgesamt (seit 2.5.0-dev18): _pd_low zaehlt nur gerade laufende
# Anfragen - zwischen ihren Schritten (Historie -> Match-Details) und vor der ersten
# rutschten sonst "Letzte 5" dazwischen
_pd_party = {"n": 0}
_pd_bulk = {"next": 0.0}
_pd_bulk_lock = threading.Lock()
_pd_counts = collections.Counter()      # pd-Anfragen im laufenden Match, je Art (fuer /api/gamestate)
_pd_count_lock = threading.Lock()


def _low_priority(fn):
    def wrapper(*a, **k):
        _tl.low = True
        try:
            return fn(*a, **k)
        finally:
            _tl.low = False
    return wrapper


def _bulk_priority(fn):
    def wrapper(*a, **k):
        prev = getattr(_tl, "bulk", False)
        _tl.bulk = True
        try:
            return fn(*a, **k)
        finally:
            _tl.bulk = prev
    return wrapper


def _pd_bulk_paused(g=None):
    """Riot-Pause oder kurz danach: "Letzte 5" fragen dann noch nicht wieder."""
    g = g or _pd_gate_read()
    now = time.time()
    return now < g["blocked_until"] or (g["last_block"] > 0 and now - g["last_block"] < PD_BULK_AFTER_BLOCK)


def _pd_bulk_wait():
    """Platz fuer eine "Letzte 5"-Anfrage. False = wegen einer Riot-Sperre (noch)
    nicht - der Aufrufer zeigt dann "Riot-Pause" und versucht es spaeter erneut."""
    deadline = time.time() + PD_BULK_MAX_WAIT
    while True:
        g = _pd_gate_read()
        now = time.time()
        paused = _pd_bulk_paused(g)
        urgent = any(sl.get("urgent") for sl in (getattr(_tl, "slots", None) or ()))
        busy = paused or (not urgent and (_pd_high["n"] > 0 or _pd_low["n"] > 0 or _pd_party["n"] > 0
                                          or now < g.get("hi_until", 0)))
        if paused and now >= deadline:
            return False
        if not busy or now >= deadline:     # wichtige Abfragen ohne Ende: im Bulk-Takt trotzdem weiter
            with _pd_bulk_lock:
                if now >= _pd_bulk["next"]:
                    _pd_bulk["next"] = now + PD_BULK_INTERVAL
                    return True
        time.sleep(0.2)


def _pd_kind(path):
    if "/match-details/" in path:
        return "details"
    if "/competitiveupdates" in path or "/match-history/" in path:
        return "history"
    if path.startswith("/mmr/"):
        return "mmr"
    if "/name-service/" in path:
        return "names"
    return "other"


def _riot_send(base_key, auth, path, method="GET", body=None):
    if base_key != "pd":
        return _http(auth[base_key] + path, auth["headers"], method=method, body=body)
    low = getattr(_tl, "low", False)
    bulk = getattr(_tl, "bulk", False) and not low
    if bulk:
        if not _pd_bulk_wait():
            _tl.bulk_denied = True
            return 429, None
    elif low:
        with _pd_count_lock:
            _pd_low["n"] += 1
        # seit dev18 auch auf vry.exe (hi_until): Namen/Raenge in dessen Tabelle zuerst
        now = time.time()
        deadline, hi_deadline = now + PD_LOW_MAX_WAIT, now + PD_LOW_HI_MAX_WAIT
        while time.time() < deadline and (
                _pd_high["n"] > 0
                or (time.time() < hi_deadline and time.time() < _pd_gate_read().get("hi_until", 0))):
            time.sleep(0.15)
    else:
        with _pd_count_lock:
            _pd_high["n"] += 1
    try:
        ticket = _pd_acquire()
        if not ticket:
            return 429, None
        info = {"kind": _pd_kind(path)}
        try:
            status, j = _http(auth[base_key] + path, auth["headers"], method=method, body=body, info=info)
        except Exception:
            _pd_feedback(None, info, probe=(ticket == "probe"))
            raise
        _pd_feedback(status, info, probe=(ticket == "probe"))
        with _pd_count_lock:
            _pd_counts[_pd_kind(path)] += 1
        return status, j
    finally:
        if low:
            with _pd_count_lock:
                _pd_low["n"] -= 1
        elif not bulk:
            with _pd_count_lock:
                _pd_high["n"] -= 1


def riot_get(base_key, path):
    auth = get_auth()
    status, j = _riot_send(base_key, auth, path)
    if status in (400, 401, 403) and not (base_key == "pd" and time.time() < _pd_gate["blocked_until"]):
        auth = get_auth(force=True)          # Token abgelaufen -> einmal erneuern
        status, j = _riot_send(base_key, auth, path)
    if status == 429 and base_key != "pd":
        # glz: kurz warten und bis zu zweimal neu versuchen. pd wartet nicht
        # hier, sondern ueber die gemeinsame Sperre in _pd_feedback() - sonst
        # wuerden mehrere Threads gleichzeitig weiter anklopfen.
        for wait in (2, 4):
            time.sleep(wait)
            status, j = _riot_send(base_key, auth, path)
            if status != 429:
                break
    return status, j


def riot_post(base_key, path, body="{}"):
    return _riot_write("POST", base_key, path, body)


def riot_put(base_key, path, body="{}"):
    return _riot_write("PUT", base_key, path, body)


def _riot_write(method, base_key, path, body):
    auth = get_auth()
    status, j = _riot_send(base_key, auth, path, method=method, body=body)
    if status in (400, 401, 403):
        auth = get_auth(force=True)
        status, j = _riot_send(base_key, auth, path, method=method, body=body)
    return status, j


AGENT_ITEM_TYPE = "01bb38e1-da47-4e6a-9b3d-945fe4655707"
ITEM_TYPES = {
    "skins": "e7c63390-eda7-46e0-bb7a-a6abdacd2433",    # liefert Skin-LEVEL-IDs
    "chromas": "3ad1b2b2-acdb-4524-852f-954a76ddae0a",  # Farbvarianten
    "buddies": "dd3bf334-87f3-40bd-b043-682a57a8dc3a",
    "sprays": "d5f120f8-ff8c-4aac-92ea-f2b5acbe9475",
    "cards": "3f296c07-64c3-494c-923b-fe692a4fa1bd",
    "titles": "de7caa6b-adf7-4588-bbd1-143831e786c6",
}
# ---- Je Account gemerkt: Besitz, Shop, Loadout, Agenten (2.5.4) ----
# Skins- und Shop-Reiter luden erst beim Anklicken. Lag gerade eine Riot-Sperre an,
# kam eine Fehlermeldung - beim Besitz sogar ok mit leeren Listen, die Seite und
# Dienst dann 10 Minuten als "besitzt nichts" behielten. Und die Zwischenspeicher
# galten nicht je Account. Jetzt: _account_warmup() holt alles im Hintergrund,
# sobald ein Account erkannt ist, der Stand liegt je Account in
# DATA_DIR/account-cache.json (gilt auch nach einem Neustart), und schlaegt eine
# Abfrage fehl, kommt der letzte Stand (stale) statt eines Fehlers.
ACCOUNT_CACHE_FILE = os.path.join(DATA_DIR, "account-cache.json")
ACCOUNT_CACHE_MAX = 6           # so viele Accounts merken
ENT_TTL = 600                   # Besitz: so lange ohne neue Abfrage
WARM_ENT_MAX_AGE = 1800         # Vorladen: Besitz nur holen, wenn aelter
_acct = {"data": None}
_acct_lock = threading.Lock()


def _acct_data():
    if _acct["data"] is None:
        _acct["data"] = _load_json(ACCOUNT_CACHE_FILE)
    return _acct["data"]


def acct_cached(puuid, kind):
    """(Zeitpunkt, Daten) des gemerkten Stands eines Accounts, sonst (0, None)."""
    with _acct_lock:
        e = (_acct_data().get(puuid) or {}).get(kind)
    if isinstance(e, list) and len(e) == 2 and isinstance(e[1], dict):
        return e[0], e[1]
    return 0.0, None


def acct_store(puuid, kind, data):
    with _acct_lock:
        d = _acct_data()
        entry = d.get(puuid) if isinstance(d.get(puuid), dict) else {}
        entry[kind] = [time.time(), data]
        entry["seen"] = time.time()
        d[puuid] = entry
        for old in sorted(d, key=lambda k: (d[k] or {}).get("seen", 0) if isinstance(d[k], dict) else 0)[
                :max(0, len(d) - ACCOUNT_CACHE_MAX)]:
            d.pop(old, None)
        try:
            _save_json(ACCOUNT_CACHE_FILE, d)
        except Exception:
            pass


def entitlements(force=False):
    """Was der Account besitzt — Grundlage fuer die Skin-Auswahl und "schon gekauft"."""
    puuid = get_auth()["puuid"]
    ts, old = acct_cached(puuid, "ent")
    if old and not force and time.time() - ts < ENT_TTL:
        return old
    out, status = {}, 200
    for key, type_id in ITEM_TYPES.items():
        st, j = riot_get("pd", "/store/v1/entitlements/%s/%s" % (puuid, type_id))
        if st == 200 and isinstance(j, dict):
            out[key] = [e.get("ItemID") for e in (j.get("Entitlements") or []) if e.get("ItemID")]
        elif key == "skins":
            status = st                  # ohne Skins taugt der Rest nichts
            break
        else:
            out[key] = (old or {}).get(key) or []
    if status == 200:
        out["ok"] = True
        acct_store(puuid, "ent", out)
        return out
    if old:
        return dict(old, stale=True)
    return {"ok": False, "error": "Besitz nicht lesbar (Status %s)." % status}


def owned_agents():
    """UUIDs der freigeschalteten Agenten — zum Ausgrauen in der Auswahl.
    Immer frisch (ein gerade freigeschalteter Agent soll sofort waehlbar sein),
    bei einem Fehlschlag der letzte bekannte Stand statt einer leeren Liste."""
    puuid = get_auth()["puuid"]
    status, j = riot_get("pd", "/store/v1/entitlements/%s/%s" % (puuid, AGENT_ITEM_TYPE))
    if status == 200 and isinstance(j, dict):
        ids = [e.get("ItemID") for e in (j.get("Entitlements") or []) if e.get("ItemID")]
        acct_store(puuid, "agents", {"ids": ids})
        return ids
    return (acct_cached(puuid, "agents")[1] or {}).get("ids") or []


# Namen aendern sich waehrend einer Sitzung praktisch nie. Bis 2.4.9-dev2 fragte
# names_for() bei JEDEM Aufruf Riot (pd) - /api/pregame tat das jede Sekunde,
# sogar zweimal (Verbuendete und Gegner). Jede dieser Abfragen musste sich in
# die gemeinsame pd-Warteschlange stellen; zu Beginn der Agentenauswahl, wenn
# gerade alle Spielerdaten laden, dauerte /api/pregame so mehrere Sekunden, die
# Seite hatte alle Browser-Verbindungen belegt, und Hovern/Sperren wartete mit
# (Nutzer-Rueckmeldung beim Live-Test 2026-10-14).
NAME_TTL = 3600
# Verborgene Namen (Streamer-Modus, "Hide my name") liefert Riot ohne GameName.
# Bis 2.4.9-dev4 kamen sie nie in den Cache und wurden bei JEDEM Poll erneut
# abgefragt (Live-Test: 64 statt 2 Namens-Anfragen in einer Agentenauswahl).
# Jetzt gemerkt, nur kuerzer; "" = Riot kennt die puuid nicht (kein Eintrag).
NAME_TTL_HIDDEN = 600
_name_cache = {}            # puuid -> (Zeitpunkt, "Name#Tag" bzw. "")
_bg_pending = set()         # ("names"|"mmr", puuid) - laeuft schon im Hintergrund
_bg_pending_lock = threading.Lock()


def _name_hidden(name):
    return not name or name.startswith("#") or name.startswith("None#")


def _names_cached(ids, now):
    out, missing = {}, []
    with _cache_lock:
        for p in ids:
            hit = _name_cache.get(p)
            if hit and now - hit[0] < (NAME_TTL_HIDDEN if _name_hidden(hit[1]) else NAME_TTL):
                if hit[1]:
                    out[p] = hit[1]
            else:
                missing.append(p)
    return out, missing


def names_for(puuids, retry_unknown=False):
    """puuid -> 'Name#Tag' (eine Anfrage fuer alle noch unbekannten).

    retry_unknown: auch gemerkte leere/verborgene Namen neu fragen (Match-Analyse,
    einmal pro Match) - sonst sperrte so ein Eintrag NAME_TTL_HIDDEN lang."""
    ids = [p for p in puuids if p]
    if not ids:
        return {}
    now = time.time()
    out, missing = _names_cached(ids, now)
    if retry_unknown:
        again = [p for p in ids if p not in missing and _name_hidden(out.get(p))]
        for p in again:
            out.pop(p, None)
        missing += again
    if missing:
        status, j = riot_put("pd", "/name-service/v2/players", json.dumps(missing))
        if status == 200 and isinstance(j, list):
            with _cache_lock:
                for e in j:
                    sub = e.get("Subject")
                    out[sub] = "%s#%s" % (e.get("GameName"), e.get("TagLine"))
                    if sub:
                        _cache_put(_name_cache, sub, (now, out[sub]))
                for p in missing:
                    if p not in out:                # von Riot gar nicht beantwortet
                        _cache_put(_name_cache, p, (now, ""))
    return out


def _in_background(kind, keys, work):
    """work(keys) einmal im Hintergrund fuer die Schluessel starten, die nicht schon laufen."""
    with _bg_pending_lock:
        todo = [k for k in keys if (kind, k) not in _bg_pending]
        _bg_pending.update((kind, k) for k in todo)
    if not todo:
        return

    def _run():
        try:
            work(todo)
        except Exception:
            pass
        finally:
            with _bg_pending_lock:
                _bg_pending.difference_update((kind, k) for k in todo)

    threading.Thread(target=_run, daemon=True).start()


def names_for_nonblocking(puuids):
    """Fuer /api/pregame: sofort die bekannten Namen, fehlende kommen im
    Hintergrund (eine Anfrage) und stehen beim naechsten 1-s-Poll da."""
    ids = [p for p in puuids if p]
    out, missing = _names_cached(ids, time.time())
    if missing:
        _in_background("names", missing, names_for)
    return out


# ============================ Match-Statistiken ============================

# Abgeschlossene Matches aendern sich nie. Statt der kompletten Match-Details
# (mehrere hundert KB je Match) wird eine Kurzfassung gecacht: die fertig
# ausgewertete Zeile jedes Spielers (siehe _extract) plus seine partyId. Damit
# teilen sich Spielerkarten, Party-Erkennung und "meistgespielter Agent"
# dieselben Matches, und der Cache kann viele davon halten. Frueher wurde er bei
# 60 Eintraegen komplett geleert - beim Matchstart (10 Spieler x 5 Matches +
# Party-Erkennung) also mitten im Laden, und alles kam ein zweites Mal von Riot.
MATCH_CACHE_MAX = 800
CACHE_MAX = 300                 # fuer die kleineren Caches unten
_match_cache = collections.OrderedDict()   # match_id -> {"rows": {puuid: row}, "party": {puuid: partyId}}
_stats_cache = {}        # puuid -> (timestamp, result)
_cache_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=3)   # Riot mag keine Anfrage-Lawine
# Party-Erkennung mit eigenem Pool: ihre Anfragen warten ggf. auf die Kacheln
# (siehe _low_priority) und duerfen dabei keine Plaetze in _pool blockieren.
_premade_pool = ThreadPoolExecutor(max_workers=3)
STATS_TTL = 300


def _cache_put(cache, key, value, limit=CACHE_MAX):
    """Eintrag setzen und bei Ueberlauf nur die aeltesten entfernen (nie alles)."""
    cache[key] = value
    while len(cache) > limit:
        cache.pop(next(iter(cache)))


def _digest_match(match):
    rows, party = {}, {}
    for p in match.get("players") or []:
        sub = p.get("subject")
        if not sub:
            continue
        party[sub] = p.get("partyId")
        row = _extract(match, sub)
        if row:
            rows[sub] = row
    return {"rows": rows, "party": party}


# Abgeschlossene Matches aendern sich nie - seit 2.4.9 ueberleben ihre
# Kurzfassungen auch einen Neustart und das naechste Match:
# DATA_DIR/match-digests.json (die juengsten MATCH_DIGEST_KEEP). Die eigenen
# letzten Matches und die der Party tauchen in jedem Spiel wieder auf und kamen
# bisher jedes Mal neu von Riot. Aendern sich _digest_match()/_extract(),
# DIGEST_VERSION erhoehen - sonst kaemen alte Kurzfassungen ohne neue Felder.
DIGEST_VERSION = 1
MATCH_DIGEST_FILE = os.path.join(DATA_DIR, "match-digests.json")
MATCH_DIGEST_KEEP = 400
_digest_disk = {"dirty": False, "saved": 0.0}


def _digests_load():
    data = _load_json(MATCH_DIGEST_FILE)
    if data.get("v") != DIGEST_VERSION or not isinstance(data.get("matches"), dict):
        return 0
    n = 0
    with _cache_lock:
        for match_id, dg in data["matches"].items():
            if isinstance(dg, dict) and isinstance(dg.get("rows"), dict) and match_id not in _match_cache:
                _cache_put(_match_cache, match_id, dg, MATCH_CACHE_MAX)
                n += 1
    return n


def _digests_save(force=False):
    if not _digest_disk["dirty"] or (not force and time.time() - _digest_disk["saved"] < 30):
        return
    with _cache_lock:
        items = list(_match_cache.items())[-MATCH_DIGEST_KEEP:]
        _digest_disk["dirty"] = False
    try:
        tmp = MATCH_DIGEST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"v": DIGEST_VERSION, "matches": dict(items)}, fh, separators=(",", ":"))
        os.replace(tmp, MATCH_DIGEST_FILE)
        _digest_disk["saved"] = time.time()
    except Exception as e:
        _digest_disk["dirty"] = True
        report_error("match-digests", e)


_inflight = {}                  # Schluessel -> {"ev": Event, "res": Ergebnis, "urgent": bool}
_inflight_lock = threading.Lock()


def _single_flight(key, fn):
    """fn() je Schluessel nur einmal gleichzeitig ausfuehren.

    Beim Matchstart wollten Spielerkarten, Vorladen, Party-Erkennung und
    "meistgespielter Agent" oft dieselben Daten im selben Moment - und jeder
    fragte Riot selbst, weil der Cache erst nach der ersten Antwort gefuellt
    war (Party-Mitglieder teilen sich ihre Matches). Wer jetzt zu spaet kommt,
    wartet auf das Ergebnis des ersten. None, wenn der erste gescheitert ist.

    Haelt eine "Letzte 5"-Abfrage den Platz und wartet eine wichtigere darauf
    (Party-Erkennung, Spieler-Fenster), wird der Platz "urgent": Die Abfrage
    stellt sich dann nicht mehr hinter Party-Erkennung/Namen an (_pd_bulk_wait),
    sonst warteten beide aufeinander, bis PD_BULK_MAX_WAIT ablief.
    """
    with _inflight_lock:
        slot = _inflight.get(key)
        owner = slot is None
        if owner:
            slot = _inflight[key] = {"ev": threading.Event(), "res": None, "urgent": False}
        elif not getattr(_tl, "bulk", False) or getattr(_tl, "low", False):
            slot["urgent"] = True
    if not owner:
        slot["ev"].wait(60)
        return slot["res"]
    held = getattr(_tl, "slots", None)
    if held is None:
        held = _tl.slots = []
    held.append(slot)
    try:
        slot["res"] = fn()
        return slot["res"]
    finally:
        held.pop()
        with _inflight_lock:
            _inflight.pop(key, None)
        slot["ev"].set()


def _match_details(match_id):
    """Kurzfassung eines abgeschlossenen Matches (siehe oben), gecacht."""
    with _cache_lock:
        hit = _match_cache.get(match_id)
        if hit is not None:
            _match_cache.move_to_end(match_id)
            return hit
    return _single_flight(("match", match_id), lambda: _match_details_fetch(match_id))


def _match_details_fetch(match_id):
    with _cache_lock:
        hit = _match_cache.get(match_id)   # kam evtl. gerade erst herein
    if hit is not None:
        return hit
    status, j = riot_get("pd", "/match-details/v1/matches/" + match_id)
    if status != 200 or not j:
        return None
    digest = _digest_match(j)
    with _cache_lock:
        _cache_put(_match_cache, match_id, digest, MATCH_CACHE_MAX)
        _digest_disk["dirty"] = True
    return digest


def _recent_match_ids(puuid, count):
    """Letzte Match-IDs eines Spielers — erst Competitive, sonst alle Modi."""
    status, j = riot_get(
        "pd", "/mmr/v1/players/%s/competitiveupdates?startIndex=0&endIndex=%d&queue=competitive"
        % (puuid, count))
    ids = []
    if status == 200 and j:
        ids = [m.get("MatchID") for m in (j.get("Matches") or []) if m.get("MatchID")]
    if not ids:
        status, j = riot_get("pd", "/match-history/v1/history/%s?startIndex=0&endIndex=%d"
                             % (puuid, count))
        if status == 200 and j:
            ids = [m.get("MatchID") for m in (j.get("History") or []) if m.get("MatchID")]
    return ids[:count]


def _extract(match, puuid):
    """Zieht die Werte eines Spielers aus einem Match-Details-Objekt."""
    me = None
    for p in match.get("players") or []:
        if p.get("subject") == puuid:
            me = p
            break
    if not me:
        return None

    st = me.get("stats") or {}
    rounds = st.get("roundsPlayed") or 0
    head = body = leg = 0
    dmg = 0
    first_kills = 0
    multi_kills = 0
    plants = defuses = 0

    for rnd in match.get("roundResults") or []:
        best_time = None
        for ps in rnd.get("playerStats") or []:
            for d in ps.get("damage") or []:
                if ps.get("subject") == puuid:
                    head += d.get("headshots") or 0
                    body += d.get("bodyshots") or 0
                    leg += d.get("legshots") or 0
                    dmg += d.get("damage") or 0
            for k in ps.get("kills") or []:
                # Feldname je nach API-Fassung: roundTime (aktuell) bzw. der lange Name
                t = k.get("roundTime")
                if t is None:
                    t = k.get("timeSinceRoundStartMillis")
                if t is not None and (best_time is None or t < best_time[0]):
                    best_time = (t, k.get("killer") or ps.get("subject"))
            if ps.get("subject") == puuid and len(ps.get("kills") or []) >= 3:
                multi_kills += 1
        if best_time and best_time[1] == puuid:
            first_kills += 1
        if rnd.get("bombPlanter") == puuid:
            plants += 1
        if rnd.get("bombDefuser") == puuid:
            defuses += 1

    shots = head + body + leg
    # Drei Zustaende statt nur True/False: wenn KEIN Team als Sieger markiert
    # ist, war es ein Unentschieden (kommt in VALORANT selten, aber
    # tatsaechlich vor) - vorher landete das faelschlich als "won=False",
    # also ununterscheidbar von einer echten Niederlage.
    teams = match.get("teams") or []
    won = None
    my_team_won = None
    for t in teams:
        if t.get("teamId") == me.get("teamId"):
            my_team_won = t.get("won")
            break
    if my_team_won:
        won = True
    elif any(t.get("won") for t in teams):
        won = False
    elif teams:
        won = "draw"
    info = match.get("matchInfo") or {}

    return {
        "matchId": info.get("matchId"),
        "map": info.get("mapId"),
        "queue": info.get("queueID") or info.get("queueId"),
        "startedAt": info.get("gameStartMillis"),
        "kills": st.get("kills") or 0,
        "deaths": st.get("deaths") or 0,
        "assists": st.get("assists") or 0,
        "score": st.get("score") or 0,
        "rounds": rounds,
        "won": won,
        "agent": me.get("characterId"),
        "tier": me.get("competitiveTier"),
        "headshots": head, "bodyshots": body, "legshots": leg,
        "hsPercent": round(head / shots * 100, 1) if shots else None,
        "damage": dmg,
        "adr": round(dmg / rounds, 1) if rounds else None,
        "acs": round((st.get("score") or 0) / rounds, 1) if rounds else None,
        "kd": round((st.get("kills") or 0) / (st.get("deaths") or 1), 2),
        "kda": round(((st.get("kills") or 0) + (st.get("assists") or 0))
                     / (st.get("deaths") or 1), 2),
        "firstKills": first_kills,
        "multiKills": multi_kills,
        "plants": plants,
        "defuses": defuses,
    }


def player_stats(puuid, count=1, season=True):
    """Letzte `count` Matches eines Spielers, plus Summe darueber.

    season=False spart den /mmr-Aufruf fuer die Saison-Bilanz - die
    Spielerkarten brauchen sie nicht (den Rang liefert vry.exe ohnehin), nur
    das Spieler-Fenster (/api/player)."""
    _snapshot_wait()
    key = "%s:%d" % (puuid, count)
    now = time.time()
    with _cache_lock:
        hit = _stats_cache.get(key)
        if hit and (now - hit[0] < STATS_TTL or _match_current(hit[0])) and (not season or "seasonGames" in hit[1]):
            return hit[1]
    # Laeuft dieselbe Abfrage schon (Vorladen, "meistgespielter Agent", zweites
    # Geraet), auf deren Ergebnis warten statt Riot doppelt zu fragen
    res = _single_flight(("stats", key, season), lambda: _player_stats_fetch(puuid, count, season, key))
    if res is None:
        return {"puuid": puuid, "error": "abgebrochen", "matches": [], "totals": None}
    return res


def _player_stats_fetch(puuid, count, season, key):
    now = time.time()
    out = {"puuid": puuid, "matches": [], "totals": None}
    incomplete = False
    _tl.bulk_denied = False
    try:
        ids = _recent_match_ids(puuid, count)
        if not ids and (pd_gate_state()["paused"] or _tl.bulk_denied):
            return {"puuid": puuid, "error": "riot-ratelimit", "matches": [], "totals": None}
        for mid in ids:
            m = _match_details(mid)
            if not m:
                incomplete = True
                continue
            row = (m.get("rows") or {}).get(puuid)
            # Frueher wurde hier auf queue=="competitive" gefiltert - das hat den
            # Nicht-Competitive-Fallback in _recent_match_ids() aber wirkungslos
            # gemacht: fuer Spieler ohne Competitive-Historie kamen dort zwar
            # echte Swiftplay/Unrated-Match-IDs zurueck, wurden hier aber sofort
            # wieder verworfen -> "Last 5 Matches" blieb leer statt die
            # tatsaechlich gespielten Modi zu zeigen (Community-Wunsch).
            if row:
                out["matches"].append(row)
    except RiotError as e:
        return {"puuid": puuid, "error": str(e), "matches": [], "totals": None}
    except Exception as e:
        return {"puuid": puuid, "error": "%s: %s" % (type(e).__name__, e),
                "matches": [], "totals": None}

    if out["matches"]:
        s = lambda f: sum(m.get(f) or 0 for m in out["matches"])
        rounds, shots = s("rounds"), s("headshots") + s("bodyshots") + s("legshots")
        wins = sum(1 for m in out["matches"] if m.get("won") is True)
        draws = sum(1 for m in out["matches"] if m.get("won") == "draw")
        out["totals"] = {
            "games": len(out["matches"]),
            "kills": s("kills"), "deaths": s("deaths"), "assists": s("assists"),
            "rounds": rounds,
            "kd": round(s("kills") / (s("deaths") or 1), 2),
            "kda": round((s("kills") + s("assists")) / (s("deaths") or 1), 2),
            "hsPercent": round(s("headshots") / shots * 100, 1) if shots else None,
            "adr": round(s("damage") / rounds, 1) if rounds else None,
            "acs": round(s("score") / rounds, 1) if rounds else None,
            "wins": wins, "draws": draws, "losses": len(out["matches"]) - wins - draws,
            "firstKills": s("firstKills"), "multiKills": s("multiKills"),
            "plants": s("plants"), "defuses": s("defuses"),
        }

    # Sieg-/Match-Bilanz der LAUFENDEN Season (Riot-eigene Zaehlung, nicht
    # aus den oben abgerufenen `count` Matches hergeleitet - kann also mehr
    # Spiele umfassen als die Stichprobe oben je zeigt).
    if season:
        try:
            sm = mmr_summary(puuid)
            out["seasonWins"] = sm.get("seasonWins")
            out["seasonGames"] = sm.get("seasonGames")
        except Exception:
            out["seasonWins"] = None
            out["seasonGames"] = None

    if incomplete and (pd_gate_state()["paused"] or _tl.bulk_denied):
        out["error"] = "riot-ratelimit"      # Teilstand anzeigen, aber nicht cachen
        return out
    with _cache_lock:
        _cache_put(_stats_cache, key, (now, out))
    return out


@_bulk_priority
def card_data(puuid):
    """Was die Spielerkarte braucht: Punktreihe (inkl. gespieltem Agent pro
    Match) + letztes Match fuer HS/KD-Luecken.

    Nutzt player_stats() fuer die "Last 5 Matches"-Reihe statt der frueheren,
    guenstigeren Abschaetzung (nur Sieg/Niederlage aus dem RR-Vorzeichen,
    kein Agent) - Community-Wunsch: die kleine Punktreihe auf der Karte soll
    den gespielten Agenten je Match zeigen statt nur eine Farbe.
    _match_details() (siehe player_stats/_extract) cacht pro Match dauerhaft
    (ein Match-Ergebnis aendert sich im Nachhinein nie), player_stats() selbst
    5 Minuten - kostet in der Praxis also nur beim allerersten Anblick eines
    neuen Spielers wirklich mehr Riot-Anfragen als vorher, nicht bei jedem
    Poll. Ersetzt auch den bisher separaten player_stats(puuid, 1)-Aufruf
    fuer "last" - der erste der 5 Matches ist exakt dasselbe Match.
    """
    st = player_stats(puuid, 5, season=False)
    matches = st.get("matches") or []
    results = [{"won": m.get("won"), "map": m.get("map"), "agent": m.get("agent")}
               for m in matches]
    # Grundlage für den Smurf-Hinweis (smurfHint() in index.html): NUR
    # Ranked-Matches. player_stats() fällt ohne Ranked-Historie auf alle Modi
    # zurück - Deathmatch & Co. hätten mit ihrer aufgeblähten K/D sonst
    # massenhaft Fehlalarme ausgelöst.
    comp = [m for m in matches if m.get("queue") == "competitive"]
    comp_totals = None
    if comp:
        k, d = sum(m.get("kills") or 0 for m in comp), sum(m.get("deaths") or 0 for m in comp)
        hs = sum(m.get("headshots") or 0 for m in comp)
        shots = hs + sum((m.get("bodyshots") or 0) + (m.get("legshots") or 0) for m in comp)
        comp_totals = {"games": len(comp), "kd": round(k / (d or 1), 2),
                       "hsPercent": round(hs / shots * 100, 1) if shots else None,
                       "wins": sum(1 for m in comp if m.get("won") is True)}
    out = {"puuid": puuid,
           "results": results,
           "last": (matches or [None])[0],
           "rankedTotals": comp_totals}
    if st.get("error"):
        out["error"] = st["error"]          # Seite versucht es spaeter erneut
    return out


# ============================ Party / Agenten / Loadout / Shop ============================

def party_info():
    auth = get_auth()
    status, p = riot_get("glz", "/parties/v1/players/%s" % auth["puuid"])
    if status != 200 or not p or not p.get("CurrentPartyID"):
        return {"ok": False, "error": "Keine Party gefunden (laeuft VALORANT?)."}
    pid = p["CurrentPartyID"]
    status, party = riot_get("glz", "/parties/v1/parties/%s" % pid)
    if status != 200 or not party:
        return {"ok": False, "error": "Party nicht lesbar (Status %s)." % status}

    members = party.get("Members") or []
    name_map = names_for([m.get("Subject") for m in members])
    out_members = []
    for m in members:
        ident = m.get("PlayerIdentity") or {}
        out_members.append({
            "puuid": m.get("Subject"),
            "name": name_map.get(m.get("Subject")) or "—",
            "owner": bool(m.get("IsOwner")),
            "ready": bool(m.get("IsReady")),
            "tier": m.get("CompetitiveTier"),
            "level": ident.get("AccountLevel"),
            "card": ident.get("PlayerCardID"),
            "me": m.get("Subject") == auth["puuid"],
        })
    mm = party.get("MatchmakingData") or {}
    return {
        "ok": True,
        "partyId": pid,
        "state": party.get("State"),                      # DEFAULT | MATCHMAKING | ...
        "accessibility": party.get("Accessibility"),      # OPEN | CLOSED
        "queue": mm.get("QueueID"),
        "eligibleQueues": party.get("EligibleQueues") or [],
        "queueTime": party.get("EstimatedQueueTimeSeconds"),
        "members": out_members,
    }


# ---- Lobby-Karten: Rang, RR und Peak für Party-Mitglieder ----
# vry.exe liest die Party NUR bei einem Zustandswechsel neu (im Log gut zu
# sehen: "new game state" -> "retrieved party members"). Wer im Menü dazukommt,
# taucht darum erst beim nächsten Match auf. Diese Daten kommen direkt von
# Riot und sind deshalb immer aktuell.

_mmr_cache = {}          # puuid -> (Zeitpunkt, Ergebnis)
MMR_TTL = 120
_seasons = {"ts": 0.0, "map": {}, "current_id": None, "previous_id": None}
# Ohne Sperre holen sich mehrere Mitglieder die Tabelle gleichzeitig: Der erste
# Thread setzt den Zeitstempel, der zweite überspringt den Abruf daraufhin und
# liest die noch leere Tabelle — dann fehlt bei einem Spieler die Act-Angabe.
_seasons_lock = threading.Lock()


ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6,
         "VII": 7, "VIII": 8, "IX": 9, "X": 10}


def _act_label(season_id):
    """Season-UUID -> Kurzform fuer die Peak-Kachel.

    Riot benennt Acts roemisch ("ACT IV") und hat die Oberkategorie inzwischen
    von "EPISODE 7" auf Jahresnamen wie "V26" umgestellt. Beides wird bedient:
    Episoden werden zu 'e7a3' (die Seite macht daraus "E7 · A3"), alles Neuere
    kommt als "V26 · A4" durch.
    """
    if not season_id:
        return None
    with _seasons_lock:
        _load_seasons()
    return _seasons["map"].get(season_id)


def _load_seasons():
    now = time.time()
    if now - _seasons["ts"] <= 86400:
        return
    _seasons["ts"] = now             # auch bei Fehlschlag nicht dauernd neu holen
    try:
        _, j = _http("https://valorant-api.com/v1/seasons")
        by_id = {s.get("uuid"): s for s in ((j or {}).get("data") or [])}
        out = {}
        # Aktuell laufende Act-Season bestimmen (fuer die Saison-Bilanz,
        # siehe mmr_summary): valorant-api liefert start-/endTime in ISO-8601
        # UTC ("...Z"), ein reiner String-Vergleich reicht dafuer.
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        current_id = None
        acts = {}                     # Act-Season -> (Start, Ende), fuer den vorherigen Act
        for sid, s in by_id.items():
            act = re.match(r"\s*ACT\s+([IVX]+)\s*$", s.get("displayName") or "", re.I)
            if not act:
                continue
            num = ROMAN.get(act.group(1).upper())
            if not num:
                continue
            parent = (by_id.get(s.get("parentUuid")) or {}).get("displayName") or ""
            ep = re.search(r"EPISODE\s*(\d+)", parent, re.I)
            if ep:
                out[sid] = "e%sa%d" % (ep.group(1), num)
            elif parent:
                out[sid] = "%s · A%d" % (parent.strip(), num)
            else:
                out[sid] = "A%d" % num
            start, end = s.get("startTime"), s.get("endTime")
            acts[sid] = (start, end)
            if start and end and start <= now_iso < end:
                current_id = sid
        # Die MMR-Antwort schlüsselt teils nach Competitive-Season statt
        # nach Act — diese Zuordnung fängt beide Schreibweisen ab.
        try:
            _, comp = _http("https://valorant-api.com/v1/seasons/competitive")
            for c in ((comp or {}).get("data") or []):
                label = out.get(c.get("seasonUuid"))
                if label and c.get("uuid"):
                    out.setdefault(c["uuid"], label)
        except Exception:
            pass
        _seasons["map"] = out
        _seasons["current_id"] = current_id
        cur_start = acts.get(current_id, (None, None))[0]
        before = [(e, sid) for sid, (_s, e) in acts.items() if e and cur_start and e <= cur_start]
        _seasons["previous_id"] = max(before)[1] if before else None
    except Exception:
        pass


def mmr_summary(puuid):
    """Aktueller Rang + RR + hoechster je erreichter Rang + Saison-Bilanz eines Spielers."""
    _snapshot_wait()
    now = time.time()
    with _cache_lock:
        hit = _mmr_cache.get(puuid)
        if hit and now - hit[0] < MMR_TTL:
            return hit[1]

    out = {"rank": None, "rr": None, "peakRank": None, "peakRankAct": None,
           "seasonWins": None, "seasonGames": None}
    try:
        status, j = riot_get("pd", "/mmr/v1/players/%s" % puuid)
    except Exception:
        return out                   # nicht cachen, beim naechsten Mal erneut
    if status != 200 or not j:
        return out                   # dito (Rate-Limit/Pause, Token) - nicht 2 Minuten "kein Rang"
    if status == 200 and j:
        last = j.get("LatestCompetitiveUpdate") or {}
        seasons = (((j.get("QueueSkills") or {}).get("competitive") or {})
                   .get("SeasonalInfoBySeasonID") or {})
        # Rang wie in vry.exe: aktueller Act; ohne Spiel im neuen Act der des
        # vorherigen (Riot uebernimmt ihn). Bis 2.4.9 kam hier immer der Stand
        # des letzten Competitive-Spiels, egal wie alt - die Lobby zeigte so
        # einen Rang, waehrend das Match (vry.exe) im neuen Act 0 zeigte.
        with _seasons_lock:
            _load_seasons()
        cur, prev = _seasons.get("current_id"), _seasons.get("previous_id")

        def act_rank(sid):
            info = seasons.get(sid) if sid else None
            if isinstance(info, dict) and (info.get("CompetitiveTier") or 0) > 2:
                return info.get("CompetitiveTier"), info.get("RankedRating")
            return None

        ranked = act_rank(cur) or act_rank(prev)
        if ranked:
            out["rank"], out["rr"] = ranked
            out["rankCarried"] = not act_rank(cur)
        elif last.get("TierAfterUpdate") is not None and (
                not cur or last.get("SeasonID") in (cur, prev)):
            # Seasons nicht ladbar oder anders verschluesselt: wie bisher
            out["rank"] = last.get("TierAfterUpdate")
            out["rr"] = last.get("RankedRatingAfterUpdate")
        best_tier, best_season = 0, None
        for sid, info in seasons.items():
            if not isinstance(info, dict):
                continue
            # WinsByTier verrät den höchsten Tier DER Act — CompetitiveTier
            # allein ist nur der Stand am Act-Ende und liegt oft darunter.
            tiers = [int(t) for t in (info.get("WinsByTier") or {}) if str(t).isdigit()]
            tier = max(tiers + [info.get("CompetitiveTier") or 0])
            if tier > best_tier:
                best_tier, best_season = tier, sid
        if best_tier:
            out["peakRank"] = best_tier
            out["peakRankAct"] = _act_label(best_season)

        # Sieg-/Match-Bilanz der AKTUELL laufenden Act-Season (nicht zu
        # verwechseln mit peakRank oben, das ist der hoechste JE erreichte
        # Rang ueber alle Seasons hinweg).
        with _seasons_lock:
            _load_seasons()
        current_id = _seasons.get("current_id")
        current_info = seasons.get(current_id) if current_id else None
        if isinstance(current_info, dict):
            out["seasonWins"] = current_info.get("NumberOfWinsWithPlacements")
            out["seasonGames"] = current_info.get("NumberOfGames")
    with _cache_lock:
        _cache_put(_mmr_cache, puuid, (now, out))
    return out


_agent_history_cache = {}   # puuid -> (Zeitpunkt, Ergebnis)
AGENT_HISTORY_TTL = 6 * 3600   # 6h - der Hauptagent eines Spielers aendert sich nicht minuetlich
AGENT_HISTORY_MATCHES = 3      # bewusst knapp: jedes Match kostet einen eigenen Match-Details-Aufruf


def mmr_summary_nonblocking(puuid):
    """Fuer /api/pregame: gecachter Rang sofort (sonst {}), Abfrage im Hintergrund."""
    now = time.time()
    with _cache_lock:
        hit = _mmr_cache.get(puuid)
        if hit and now - hit[0] < MMR_TTL:
            return hit[1]
    _in_background("mmr", [puuid], lambda ids: [mmr_summary(u) for u in ids])
    return {}


@_bulk_priority
def most_played_agent(puuid):
    """Meistgespielter Agent der letzten paar Matches - Idee aus der Community:
    schon in der Agentenauswahl einen groben Hinweis geben, was ein Spieler
    (vor allem ein Gegner) tendenziell spielt.

    Nutzt seit 2.4.4 dieselben Daten wie die "Letzte 5 Matches"-Reihe der
    Spielerkarte (player_stats), statt eigene Match-Details zu holen - beim
    Matchstart sind die fuer Gegner ohnehin gleich danach dran. Liefert None,
    sobald irgendetwas fehlschlaegt - nie ein Grund, die restliche Anzeige zu
    verzoegern oder abzubrechen.
    """
    _snapshot_wait()
    now = time.time()
    with _cache_lock:
        hit = _agent_history_cache.get(puuid)
        if hit and now - hit[0] < AGENT_HISTORY_TTL:
            return hit[1]

    result = None
    try:
        st = player_stats(puuid, 5, season=False)
        if st.get("error"):
            return None                     # nicht cachen, spaeter erneut
        matches = (st.get("matches") or [])[:AGENT_HISTORY_MATCHES]
        counts = {}
        for m in matches:
            cid = m.get("agent")
            if cid:
                counts[cid] = counts.get(cid, 0) + 1
        if counts:
            best = max(counts, key=counts.get)
            result = {"agent": best, "count": counts[best], "games": len(matches)}
    except Exception:
        result = None

    with _cache_lock:
        _cache_put(_agent_history_cache, puuid, (now, result))
    return result


_agent_history_pending = set()
_agent_history_pending_lock = threading.Lock()


def most_played_agent_nonblocking(puuid):
    """Nicht-blockierende Fassung fuer die Agentenauswahl: liefert sofort den
    gecachten Stand (oder None, wenn noch nichts bekannt ist), statt Rang und
    Skins in _pregame_loadouts_enemies auf bis zu vier zusaetzliche
    Riot-Aufrufe pro Spieler warten zu lassen. Stoesst hoechstens einmal pro
    Puuid eine Hintergrundberechnung an; der naechste 1s-Poll von
    /api/pregame zeigt das Ergebnis von selbst, sobald es da ist.
    """
    now = time.time()
    with _cache_lock:
        hit = _agent_history_cache.get(puuid)
        if hit and now - hit[0] < AGENT_HISTORY_TTL:
            return hit[1]
    with _agent_history_pending_lock:
        if puuid in _agent_history_pending:
            return None
        _agent_history_pending.add(puuid)

    def _compute():
        try:
            most_played_agent(puuid)   # schreibt sein Ergebnis selbst in _agent_history_cache
        finally:
            with _agent_history_pending_lock:
                _agent_history_pending.discard(puuid)

    threading.Thread(target=_compute, daemon=True).start()
    return None


def lobby_info():
    """Party-Info wie /api/party, aber mit allem, was eine Lobby-Karte braucht."""
    info = party_info()
    if not info.get("ok"):
        return info
    members = info.get("members") or []
    ids = [m["puuid"] for m in members if m.get("puuid")]
    ranks = dict(zip(ids, _pool.map(mmr_summary, ids))) if ids else {}
    out = []
    for m in members:
        r = ranks.get(m.get("puuid")) or {}
        rank = r.get("rank")
        out.append(dict(m,
                        # CompetitiveTier der Party als Rückfall, falls die
                        # MMR-Abfrage nichts hergibt (neue Accounts, Aussetzer)
                        rank=rank if rank is not None else m.get("tier"),
                        rr=r.get("rr"),
                        peakRank=r.get("peakRank"),
                        peakRankAct=r.get("peakRankAct"), rankCarried=bool(r.get("rankCarried"))))
    return dict(info, members=out)


def _party_id():
    auth = get_auth()
    status, p = riot_get("glz", "/parties/v1/players/%s" % auth["puuid"])
    if status != 200 or not p:
        return None
    return p.get("CurrentPartyID")


def party_action(action, payload):
    auth = get_auth()
    pid = _party_id()
    if not pid:
        return {"ok": False, "error": "Keine Party gefunden."}

    if action == "queue":
        q = (payload or {}).get("queue")
        if not q:
            return {"ok": False, "error": "Kein Modus angegeben."}
        status, _ = riot_post("glz", "/parties/v1/parties/%s/queue" % pid,
                              json.dumps({"queueID": q}))
    elif action == "start":
        status, _ = riot_post("glz", "/parties/v1/parties/%s/matchmaking/join" % pid)
    elif action == "stop":
        status, _ = riot_post("glz", "/parties/v1/parties/%s/matchmaking/leave" % pid)
    elif action == "ready":
        val = bool((payload or {}).get("ready", True))
        status, _ = riot_post("glz", "/parties/v1/parties/%s/members/%s/setReady"
                              % (pid, auth["puuid"]), json.dumps({"ready": val}))
    elif action == "access":
        openp = bool((payload or {}).get("open"))
        status, _ = riot_post("glz", "/parties/v1/parties/%s/accessibility" % pid,
                              json.dumps({"accessibility": "OPEN" if openp else "CLOSED"}))
    elif action == "leave":
        status, _ = riot_post("glz", "/parties/v1/players/%s/leaveparty" % auth["puuid"])
    else:
        return {"ok": False, "error": "Unbekannte Aktion."}

    if status in (200, 204):
        return {"ok": True}
    return {"ok": False, "error": "Riot lehnte ab (Status %s)." % status}


def pick_agent(mid, kind, agent_id):
    """Hovern/Sperren in einer BEKANNTEN Agentenauswahl -> (ok, Fehlertext).

    Bewusst mit ausdruecklicher Match-ID: Der Instalock kennt die frische ID
    und darf nicht ueber den 60-Sekunden-Cache in ein altes Match greifen.
    """
    status, _ = riot_post("glz", "/pregame/v1/matches/%s/%s/%s" % (mid, kind, agent_id))
    if status in (200, 204):
        _pregame_invalidate()   # eigene Wahl sofort sichtbar machen
        return True, ""
    if status == 400:
        return False, ("Agent nicht waehlbar (gesperrt, nicht freigeschaltet "
                       "oder Auswahl vorbei).")
    return False, "Riot lehnte ab (Status %s)." % status


def agent_action(kind, agent_id):
    """kind: 'select' (nur hovern) oder 'lock' (fest waehlen).

    Beides erst MANUAL_LOCK_MIN nach Beginn der Auswahl - dieselbe Schutzzeit wie
    beim Instalock, sonst waere das Overlay/Handy ein Sofort-Pick-Tool (Nutzerwunsch
    2.5.4-dev6: auch Hovern). Dafuer immer mit frischer ID: Nach einem Dodge zaehlt
    die neue Auswahl ab ihrem Beginn, nicht die alte aus dem 60-s-Cache."""
    if not agent_id:
        return {"ok": False, "error": "Kein Agent angegeben."}
    mid = pregame_match_id(force=True)
    if not mid:
        return {"ok": False, "error": "Gerade keine Agentenauswahl aktiv."}
    wait = manual_lock_wait(mid)
    if 0 < wait <= MANUAL_LOCK_GRACE:
        time.sleep(wait)            # Knopf und Dienst-Uhr laufen nicht auf die Zehntel gleich
    elif wait > 0:
        return {"ok": False, "code": "lock-too-early", "wait": round(wait, 1),
                "error": "Agent waehlen geht erst %d s nach Beginn der Agentenauswahl (noch %d s)."
                         % (MANUAL_LOCK_MIN, math.ceil(wait))}
    ok, err = pick_agent(mid, kind, agent_id)
    return {"ok": True} if ok else {"ok": False, "error": err}


def get_loadout(cached=False):
    """Aktuelles Loadout. cached=True (Vorladen der Seite): der gemerkte Stand, wenn
    es einen gibt - die Seite holt beim Oeffnen des Reiters ohnehin frisch."""
    auth = get_auth()
    ts, old = acct_cached(auth["puuid"], "loadout")
    if cached and old:
        return dict(old, ok=True, cached=True, age=int(time.time() - ts))
    status, j = riot_get("pd", "/personalization/v3/players/%s/playerloadout" % auth["puuid"])
    if (status != 200 or not j) and old:
        return dict(old, ok=True, stale=True, age=int(time.time() - ts))   # Riot zickt -> letzter Stand
    if status != 200 or not j:
        # Riot schickt bei einer Ablehnung meist einen JSON-Body mit
        # errorCode/message mit - der wurde bisher verworfen, sodass ein
        # Fehler nur als bedeutungsloser Status-Code sichtbar war.
        detail = None
        if isinstance(j, dict):
            detail = j.get("message") or j.get("errorCode")
        msg = "Loadout nicht lesbar (Status %s)." % status
        if detail:
            msg += " " + str(detail)
        return {"ok": False, "error": msg}
    acct_store(auth["puuid"], "loadout", j)
    return dict(j, ok=True)


def put_loadout(body):
    auth = get_auth()
    if not isinstance(body, dict) or not body.get("Guns"):
        return {"ok": False, "error": "Ungueltiges Loadout."}
    # Sprays/Identität immer aus dem AKTUELLEN Loadout übernehmen, damit ein
    # Preset sie nicht überschreibt — auch nicht aus alten Preset-Dateien.
    current = riot_get("pd", "/personalization/v3/players/%s/playerloadout" % auth["puuid"])[1] or {}
    payload = {
        "Guns": body.get("Guns") or [],
        "Sprays": current.get("Sprays") or body.get("Sprays") or [],
        "Identity": current.get("Identity") or body.get("Identity") or {},
        "Incognito": bool(current.get("Incognito", body.get("Incognito"))),
    }
    status, j = riot_put("pd", "/personalization/v3/players/%s/playerloadout" % auth["puuid"],
                         json.dumps(payload))
    if status in (200, 204):
        record_feature("skin_applied")
        acct_store(auth["puuid"], "loadout",
                   j if isinstance(j, dict) and j.get("Guns") else dict(current, **payload))
        return {"ok": True}
    if status == 400:
        return {"ok": False, "error": "Riot hat das Loadout abgelehnt — im laufenden Match "
                                      "lassen sich Skins nicht wechseln."}
    return {"ok": False, "error": "Riot lehnte ab (Status %s)." % status}


PRESETS_FILE = os.path.join(DATA_DIR, "vry_presets.json")
ENCOUNTERS_FILE = os.path.join(DATA_DIR, "vry_encounters.json")
CONFIG_JSON_FILE = os.path.join(DATA_DIR, "config.json")
_presets_lock = threading.Lock()
_store_lock = threading.Lock()
_config_lock = threading.Lock()

RPC_LANGS = ("de", "en", "pl", "fr", "es", "tr", "ko", "pt", "ru", "ja")


def set_rpc_lang(lang, auto=False, local=True):
    """Schreibt die App-Sprache in config.json, damit vry.exe (Discord Rich
    Presence, siehe lib/src/rpc.py) sie unabhaengig vom Browser mitliest.

    auto=True ist der Abgleich beim blossen Laden der Seite. Seit 2.4.9 zaehlt
    der nur vom PC selbst und nur, solange nie ausdruecklich eine Sprache
    gewaehlt wurde ("langExplicit") - vorher ueberschrieb jedes Geraet, das
    die Seite nur ansah (gekoppeltes Handy, zweiter Browser), die Sprache."""
    lang = str(lang or "").lower()
    if lang not in RPC_LANGS:
        return {"ok": False, "error": "Unbekannte Sprache."}
    with _config_lock:
        data = _load_json(CONFIG_JSON_FILE)
        if auto and (not local or data.get("langExplicit")):
            return {"ok": True, "ignored": True}
        if data.get("lang") == lang and (auto or data.get("langExplicit")):
            return {"ok": True}
        data["lang"] = lang
        if not auto:
            data["langExplicit"] = True
        _save_json(CONFIG_JSON_FILE, data)
    return {"ok": True}


def set_perf_mode(on, local=True):
    """Performance-Modus des Overlays (2.5.4) zusaetzlich in config.json. Die Seite
    merkt ihn sich im localStorage, der gilt aber je Adresse (http://rankyoinker/
    und localhost:1101 getrennt) und ist nach dem Leeren der Browserdaten weg.
    Nur vom PC selbst - ein gekoppeltes Handy hat seinen eigenen Schalter."""
    if not local:
        return {"ok": True, "ignored": True}
    on = on is True
    with _config_lock:
        data = _load_json(CONFIG_JSON_FILE)
        if data.get("perfMode") is on:
            return {"ok": True}
        data["perfMode"] = on
        _save_json(CONFIG_JSON_FILE, data)
    return {"ok": True}


def _load_json(path):
    """utf-8-sig, damit auch eine von Hand gespeicherte Datei mit BOM lesbar ist."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        try:
            os.replace(path, path + ".broken")   # kaputte Datei nicht ueberschreiben
        except Exception:
            pass
        return {}


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------- Begegnungen ----------------------------

def _enc_account():
    """Datensatz des eingeloggten Accounts: {players:{}, matches:[]}"""
    me = get_auth()["puuid"]
    root = _load_json(ENCOUNTERS_FILE)
    acc = root.get(me)
    if not isinstance(acc, dict):
        acc = {"players": {}, "matches": []}
        root[me] = acc
    acc.setdefault("players", {})
    acc.setdefault("matches", [])
    return root, acc


def encounters_for(puuids):
    # Notizen gibt es nicht mehr — alte Einträge in der Datei bleiben zwar
    # erhalten, werden aber nicht mehr ausgeliefert.
    with _store_lock:
        _, acc = _enc_account()
        out = {p: {k: v for k, v in acc["players"][p].items() if k != "note"}
               for p in puuids if p in acc["players"]}
    return {"ok": True, "players": out}


# Je Spieler werden ausser den Zählern die letzten Begegnungen einzeln
# mitgeschrieben — daraus baut die Grossansicht ihre Liste. Kurze Schlüssel,
# weil das je Spieler und Begegnung anfällt:
#   t = Zeitpunkt (Unix), a = 1 wenn im eigenen Team, m = MapID, q = Queue
ENC_LOG_MAX = 20


def encounters_record(match_id, players, meta=None):
    """Zaehlt eine Begegnung je Mitspieler. Doppelte Aufrufe fuer dieselbe
    Match-ID werden ignoriert, damit ein Neuzeichnen nicht mitzaehlt."""
    if not match_id or not isinstance(players, list):
        return {"ok": False, "error": "match_id oder Spielerliste fehlt."}
    meta = meta or {}
    with _store_lock:
        root, acc = _enc_account()
        if match_id in acc["matches"]:
            return {"ok": True, "skipped": True}
        me = get_auth()["puuid"]
        acc["matches"].append(match_id)
        acc["matches"] = acc["matches"][-400:]
        now = int(time.time())
        for p in players:
            pid = p.get("puuid")
            if not pid or pid == me:
                continue
            e = acc["players"].setdefault(pid, {"games": 0, "ally": 0, "enemy": 0})
            e["games"] = e.get("games", 0) + 1
            ally = bool(p.get("ally"))
            if ally:
                e["ally"] = e.get("ally", 0) + 1
            else:
                e["enemy"] = e.get("enemy", 0) + 1
            if p.get("name"):
                e["name"] = p["name"]
            e["last"] = now
            entry = {"t": now, "a": 1 if ally else 0}
            if meta.get("map"):
                entry["m"] = meta["map"]
            if meta.get("queue"):
                entry["q"] = meta["queue"]
            log = e.setdefault("log", [])
            log.append(entry)
            e["log"] = log[-ENC_LOG_MAX:]
        _save_json(ENCOUNTERS_FILE, root)
    return {"ok": True}


# Früher zählte die Seite die Begegnungen: Sie hatte den Kader ohnehin auf dem
# Schirm und schickte ihn an /api/encounters/record. War der Tab zu (oder zeigte
# er einen anderen Reiter), fiel das Match ersatzlos aus der Statistik. Darum
# holt sich der Dienst den Kader jetzt selbst.

_enc_seen = {"match": None}


def _core_game_roster(mid):
    """Alle Spieler eines laufenden Matches — mit Team, Namen, Karte und Modus."""
    auth = get_auth()
    status, m = riot_get("glz", "/core-game/v1/matches/%s" % mid)
    if status != 200 or not m:
        return None
    players = [p for p in (m.get("Players") or []) if p.get("Subject")]
    if not players:
        return None
    me = auth["puuid"]
    my_team = next((p.get("TeamID") for p in players if p.get("Subject") == me), None)
    names = names_for([p["Subject"] for p in players])
    return {
        "players": [{"puuid": p["Subject"],
                     "ally": p.get("TeamID") == my_team,
                     "name": names.get(p["Subject"]),
                     # fuer die Lade-Ansicht (Agentenbild je Platz, ohne Namen)
                     "agent": (p.get("CharacterID") or "").lower() or None}
                    for p in players],
        # MapID als Pfad ("/Game/Maps/Bonsai/Bonsai") — den Anzeigenamen löst
        # die Seite selbst auf, die hat die Kartenliste ohnehin schon geladen.
        "map": (m.get("MapID") or "").lower(),
        "queue": (m.get("MatchmakingData") or {}).get("QueueID") or "",
    }


def record_encounter_now(mid):
    """Begegnungen eines laufenden Matches zaehlen — ganz ohne offene Seite.

    Erst wenn es wirklich geklappt hat, gilt das Match als erledigt; sonst
    versucht es der Waechter beim naechsten Durchlauf noch einmal.
    """
    if not mid or _enc_seen["match"] == mid:
        return False
    try:
        roster = _core_game_roster(mid)
        if not roster:
            return False
        # Gleich mit merken: die Lade-Ansicht zeigt damit Karte und Agenten,
        # waehrend vry.exe noch die Raenge holt - ohne eigene Riot-Anfrage.
        _game_roster.update({"match": mid, "map": roster["map"], "queue": roster["queue"],
                             "players": [{"puuid": p["puuid"], "ally": p["ally"], "agent": p.get("agent")}
                                         for p in roster["players"]]})
        res = encounters_record(mid, roster["players"],
                                {"map": roster["map"], "queue": roster["queue"]})
    except Exception:
        return False
    _enc_seen["match"] = mid
    # Stand schon in der Datei (z. B. Dienst neu gestartet) -> nichts gezählt
    return bool(res.get("ok")) and not res.get("skipped")


# ---------------------------- Party-Erkennung ----------------------------
# Wer spielt mit wem? Was Riot dazu live verraet (im laufenden Match
# nachgeprueft, 2026-10-13):
#  - /core-game/v1/matches/<id> und /pregame/v1/matches/<id> enthalten KEINE
#    partyId, und die Match-Details des laufenden Matches gibt es erst nach
#    dem Abpfiff (404).
#  - /chat/v4/presences enthaelt nur Freunde (und einen selbst) - fuer die
#    aber live mit partyId und partySize. vry.exe faerbt nur diese ein.
#  - Die eigene Party kommt komplett ueber /parties/v1/parties/<eigene id>.
#  - Fremde Partys: /parties/v1/players/<fremde puuid> antwortet mit 403
#    NONSELF_OPERATION. /parties/v1/parties/<fremde partyId> nennt zwar auch
#    keine Mitglieder, unterscheidet aber: 403 PLAYER_NOT_IN_PARTY = die Party
#    gibt es noch, 404 PARTY_DNE = aufgeloest.
#
# Dazu steht in den ABGESCHLOSSENEN Matches je Spieler eine partyId. Daraus
# entstehen drei Stufen je Gruppe:
#   live    - direkt von Riot: die eigene Party, Freunde mit gleicher partyId
#   alive   - im jeweils letzten Match in derselben Party, und die besteht laut
#             Riot jetzt noch -> praktisch sicher
#   history - in einem der letzten Matches zusammen in einer Party (Schaetzung)
# Gegenbeweise zaehlen auch: Wer in einer vollstaendig bekannten Party sitzt
# (eigene, oder Freund samt partySize), bekommt keine geschaetzten Mitspieler
# dazu. Und hat jeder von zwei Spielern seit dem gemeinsamen Match in einer
# anderen Party gespielt, die beide noch bestehen, sind sie heute getrennt
# unterwegs - man sitzt immer in genau einer Party.
#
# Grenzen (bewusst): Ein Duo, das zum allerersten Mal zusammen spielt, hat noch
# keine gemeinsame Historie und bleibt unerkannt, solange keiner davon ein
# Freund ist.

_premade_cache = {"match": None, "ts": 0.0, "data": None}
PREMADE_TTL = 900          # ein Kader aendert sich waehrend eines Matches nicht
PREMADE_HISTORY = 10       # so viele vergangene Matches je Spieler
PREMADE_DETAILS = 12       # Obergrenze der Match-Details-Abfragen
PREMADE_MAX_PARTY = 5
_PREMADE_SOURCES = ("live", "alive", "history")   # Index = Stufe, kleiner = sicherer


def _lobby_roster():
    """Kader des laufenden Matches bzw. der Agentenauswahl: puuid + Team."""
    auth = get_auth()
    status, j = riot_get("glz", "/core-game/v1/players/%s" % auth["puuid"])
    if status == 200 and j and j.get("MatchID"):
        mid = j["MatchID"]
        status, m = riot_get("glz", "/core-game/v1/matches/%s" % mid)
        if status == 200 and m:
            roster = [{"puuid": p["Subject"], "team": p.get("TeamID") or ""}
                      for p in (m.get("Players") or []) if p.get("Subject")]
            if roster:
                return mid, "INGAME", roster
    mid = pregame_match_id()
    if mid:
        status, m = riot_get("glz", "/pregame/v1/matches/%s" % mid)
        if status == 200 and m:
            raw = (m.get("AllyTeam") or {}).get("Players") or []
            ally_puuids = [p["Subject"] for p in raw if p.get("Subject")]
            roster = [{"puuid": u, "team": "ally"} for u in ally_puuids]
            # Gegner-Puuids ueber denselben Loadouts-Endpunkt wie in _pregame_fetch
            # (siehe _pregame_loadouts_by_puuid) - damit sehen Begegnungs-Abzeichen
            # und Partyerkennung in der Agentenauswahl auch das gegnerische Team,
            # nicht nur die eigene Seite.
            loadouts_by_puuid = _pregame_loadouts_by_puuid(mid)
            if loadouts_by_puuid:
                skip = set(ally_puuids) | {auth["puuid"]}
                roster += [{"puuid": u, "team": "enemy"}
                           for u in loadouts_by_puuid if u not in skip]
            if roster:
                return mid, "PREGAME", roster
    return None, "MENUS", []


def _own_party():
    """Die eigene Party direkt von Riot: (partyId, [puuids]) - vollstaendig."""
    try:
        pid = _party_id()
        if not pid:
            return None, []
        status, party = riot_get("glz", "/parties/v1/parties/%s" % pid)
        if status != 200 or not party:
            return None, []
        return pid, [m.get("Subject") for m in (party.get("Members") or []) if m.get("Subject")]
    except Exception:
        return None, []


def _riot_local_get(path):
    """GET gegen die lokale Riot-Client-API (Zugang aus dem Lockfile)."""
    if not os.path.exists(LOCKFILE):
        return None, None
    with open(LOCKFILE, "r", encoding="utf-8", errors="replace") as fh:
        parts = fh.read().strip().split(":")
    if len(parts) < 5:
        return None, None
    basic = base64.b64encode(("riot:" + parts[3]).encode()).decode()
    return _http("https://127.0.0.1:%s%s" % (parts[2], path),
                 {"Authorization": "Basic " + basic}, timeout=5, local_unverified=True)


def _presence_parties(puuids):
    """puuid -> (partyId, partySize) aus den Chat-Presences - gibt es nur fuer Freunde.

    Dieselbe Quelle, die vry.exe einfaerbt, aber samt partySize: damit ist auch
    klar, ob ein Freund solo spielt oder wie viele Unbekannte zu ihm gehoeren.
    """
    wanted, out = set(puuids), {}
    try:
        status, j = _riot_local_get("/chat/v4/presences")
    except Exception:
        return out
    if status != 200 or not isinstance(j, dict):
        return out
    for pr in j.get("presences") or []:
        pu, priv = pr.get("puuid"), pr.get("private")
        if pu not in wanted or pr.get("product") != "valorant" or not priv or "{" in str(priv):
            continue
        try:
            d = json.loads(base64.b64decode(priv))
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("isValid"):
            continue
        ppd = d.get("partyPresenceData") or {}     # neue, verschachtelte Struktur
        pid = ppd.get("partyId") or d.get("partyId")
        try:
            size = int(ppd.get("partySize") or d.get("partySize") or 0)
        except (TypeError, ValueError):
            size = 0
        if pid:
            out[pu] = (pid, size)
    return out


def _party_alive(party_id):
    """Gibt es diese Party noch? True / False / None (unklar).

    Bewusst ueber _riot_send statt riot_get: riot_get haelt ein 403 fuer einen
    abgelaufenen Token und holt dann frische Token samt ShooterGame.log - hier
    ist das 403 aber die eigentliche Antwort (siehe Kopfkommentar).
    """
    try:
        status, j = _riot_send("glz", get_auth(), "/parties/v1/parties/%s" % party_id)
    except Exception:
        return None
    code = j.get("errorCode") if isinstance(j, dict) else None
    if status == 200 or (status == 403 and code == "PLAYER_NOT_IN_PARTY"):
        return True
    if status == 404 and code == "PARTY_DNE":
        return False
    return None


_premade_hist_cache = {}       # puuid -> (Zeitpunkt, [match_ids])
PREMADE_HIST_TTL = 900


def _premade_history(puuid):
    """Letzte Match-IDs eines Spielers — ueber alle Modi, nicht nur Competitive.

    competitiveupdates funktioniert im Gegensatz zu /parties auch fuer fremde
    puuids; ohne queue-Filter kommen auch Unrated-Matches mit. Gecacht: die
    Erkennung laeuft in der Agentenauswahl und beim Matchstart noch einmal (dann
    mit den Gegnern, siehe premades()) - weitgehend mit denselben Spielern.
    """
    now = time.time()
    with _cache_lock:
        hit = _premade_hist_cache.get(puuid)
        if hit and (now - hit[0] < PREMADE_HIST_TTL or _match_current(hit[0])):
            return hit[1]
    ids = _premade_history_fetch(puuid)
    if ids:
        with _cache_lock:
            _cache_put(_premade_hist_cache, puuid, (now, ids))
    return ids


def _premade_history_fetch(puuid):
    try:
        status, j = riot_get(
            "pd", "/mmr/v1/players/%s/competitiveupdates?startIndex=0&endIndex=%d"
            % (puuid, PREMADE_HISTORY))
    except Exception:
        return []
    if status != 200 or not j:
        return []
    return [m.get("MatchID") for m in (j.get("Matches") or []) if m.get("MatchID")]


def _premade_details(match_id):
    """Match-Details, aber ein Ausrutscher kippt nicht die ganze Auswertung."""
    try:
        return _match_details(match_id)
    except Exception:
        return None


def _premades_build(mid, state, roster):
    puuids = [p["puuid"] for p in roster]
    team = {p["puuid"]: p["team"] for p in roster}
    seat = {p: i for i, p in enumerate(puuids)}
    # Union-Find; je Wurzel: Anzahl, Hoechstgroesse und schwaechste Stufe der Gruppe
    parent = {p: p for p in puuids}
    count = {p: 1 for p in puuids}
    cap = {p: PREMADE_MAX_PARTY for p in puuids}
    level = {p: 0 for p in puuids}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b, lvl):
        ra, rb = find(a), find(b)
        if ra == rb:
            return True
        if count[ra] + count[rb] > min(cap[ra], cap[rb]):
            return False                      # passt nicht mehr in eine Party
        parent[rb] = ra
        count[ra] += count[rb]
        cap[ra] = min(cap[ra], cap[rb])
        level[ra] = max(level[ra], level[rb], lvl)
        return True

    # 1) live: eigene Party (vollstaendig) + Freunde aus den Presences
    known = {}                                # partyId -> {"members", "size", "own"}
    own_pid, own_members = _own_party()
    if own_pid:
        known[own_pid] = {"members": {p for p in own_members if p in parent},
                          "size": len(own_members), "own": True}
    for pu, (pid, size) in _presence_parties(puuids).items():
        k = known.setdefault(pid, {"members": set(), "size": 0, "own": False})
        k["members"].add(pu)
        if not k["own"]:
            k["size"] = max(k["size"], size)
    fixed, size_of = {}, {}                   # puuid -> partyId bzw. bekannte Partygroesse
    for pid, k in known.items():
        members = sorted(k["members"], key=lambda x: seat[x])
        if not members:
            continue
        for pu in members:
            fixed[pu] = pid
            size_of[pu] = max(k["size"], len(members))
        for other in members[1:]:
            union(members[0], other, 0)
        if k["size"]:
            root = find(members[0])
            cap[root] = min(cap[root], max(k["size"], len(members)))

    # 2) Historie nur fuer Spieler, deren Party noch nicht vollstaendig bekannt ist
    todo = [p for p in puuids if count[find(p)] < cap[find(p)]]
    hist = dict(zip(todo, _premade_pool.map(_low_priority(_premade_history), todo))) if len(todo) > 1 else {}
    seen, freshness, latest = {}, {}, {}
    for pu, ids in hist.items():
        ids = [m for m in ids if m != mid]    # das laufende Match zaehlt nicht
        if ids:
            latest[pu] = ids[0]
        for i, m in enumerate(ids):
            seen.setdefault(m, set()).add(pu)
            freshness[m] = min(freshness.get(m, 99), i)

    shared = sorted((m for m, who in seen.items() if len(who) > 1),
                    key=lambda m: (freshness[m], m))[:PREMADE_DETAILS]
    details = dict(zip(shared, _premade_pool.map(_low_priority(_premade_details), shared))) if shared else {}

    pairs = {}                                # (a, b) -> Zahl der Matches in derselben Party
    for m in shared:
        party_of = (details.get(m) or {}).get("party") or {}
        by_party = {}
        for pu in seen[m]:
            if party_of.get(pu):
                by_party.setdefault(party_of[pu], []).append(pu)
        for members in by_party.values():
            members.sort(key=lambda x: seat[x])
            for i in range(len(members)):
                for k in range(i + 1, len(members)):
                    a, b = members[i], members[k]
                    if team.get(a) != team.get(b):
                        continue              # verschiedene Teams -> heute keine Party
                    pairs[(a, b)] = pairs.get((a, b), 0) + 1

    # 3) Besteht die Party aus dem jeweils letzten Match noch?
    involved = {p for pair in pairs for p in pair}
    need = sorted({latest[p] for p in involved if p in latest} - set(details))
    if need:
        details.update(zip(need, _premade_pool.map(_low_priority(_premade_details), need)))
    last_party, last_members = {}, {}         # puuid -> partyId bzw. Mitglieder im letzten Match
    for p in involved:
        party_of = (details.get(latest.get(p)) or {}).get("party") or {}
        if party_of.get(p):
            last_party[p] = party_of[p]
            last_members[p] = {q for q, x in party_of.items() if x == party_of[p]}
    pids = sorted(set(last_party.values()))
    alive = dict(zip(pids, _pool.map(_party_alive, pids))) if pids else {}

    def anchor(p):
        """Party, in der p jetzt sicher sitzt - oder None.

        Live bekannt, oder die Party aus dem letzten Match besteht noch und
        alle damaligen Mitspieler stehen heute im selben Team (sonst koennte
        auch p gegangen und der Rest geblieben sein).
        """
        if p in fixed:
            return fixed[p]
        pid = last_party.get(p)
        if pid and alive.get(pid) is True and all(team.get(q) == team[p] for q in last_members[p]):
            return pid
        return None

    links = []
    for (a, b), n in pairs.items():
        xa, xb = anchor(a), anchor(b)
        if xa and xb:
            if xa != xb:
                continue                      # sitzen heute nachweislich in verschiedenen Partys
            lvl = 1
        elif last_party.get(a) and last_party.get(a) == last_party.get(b):
            lvl = 1 if alive.get(last_party[a]) is True else 2
        else:
            lvl = 2
        links.append((lvl, -n, seat[a], seat[b], a, b))
    for lvl, _, _, _, a, b in sorted(links):  # sichere Verbindungen zuerst
        union(a, b, lvl)

    # 4) Gruppen durchnummerieren: die eigene Party zuerst, dann nach Kaderplatz
    buckets = {}
    for pu in puuids:
        buckets.setdefault(find(pu), []).append(pu)
    me = get_auth()["puuid"]
    groups = []
    for root, members in buckets.items():
        members.sort(key=lambda x: seat[x])
        size = max([len(members)] + [size_of.get(pu, 0) for pu in members])
        if size > 1:                          # auch ein Freund, dessen Mitspieler fremd sind
            groups.append((root, members, size))
    groups.sort(key=lambda g: (0 if me in g[1] else 1, seat[g[1][0]]))

    parties, out_groups = {}, []
    for number, (root, members, size) in enumerate(groups, 1):
        for pu in members:
            parties[pu] = number
        out_groups.append({
            "party": number,
            "members": members,
            "team": team.get(members[0]) or "",
            "size": size,                     # > len(members): Rest der Party unbekannt
            "source": _PREMADE_SOURCES[level[root]],
            "proof": max([n for (a, b), n in pairs.items() if a in members and b in members] or [0]),
        })

    return {"ok": True, "matchId": mid, "state": state, "parties": parties,
            "groups": out_groups, "checked": len(details), "aliveChecked": len(alive),
            "at": int(time.time())}


def premades():
    """Wer spielt hier mit wem zusammen? — auch bei fremden Spielern."""
    _snapshot_wait()
    mid, state, roster = _lobby_roster()
    if not roster:
        return {"ok": False, "error": "Gerade kein laufendes Match."}
    # Schluessel = Match-ID UND Kader: Agentenauswahl und Match haben dieselbe
    # Match-ID (live nachgeprueft, 2026-10-13), in der Agentenauswahl fehlen die
    # Gegner aber oft. Nur mit der ID hielt das Ergebnis aus der Agentenauswahl
    # bis zu 15 min - die gegnerischen Partys fehlten dann im ganzen Match.
    key = "%s|%s" % (mid, ",".join(sorted(p["puuid"] for p in roster)))
    now = time.time()
    with _cache_lock:
        hit = _premade_cache
        if hit["match"] == key and hit["data"] and (now - hit["ts"] < PREMADE_TTL or _match_current(hit["ts"])):
            return hit["data"]
    # Vorladen (siehe _prefetch_match) und Seite fragen oft gleichzeitig
    res = _single_flight(("premades", key), lambda: _premades_store(key, mid, state, roster))
    return res if res is not None else {"ok": False, "error": "abgebrochen"}


def _premades_store(key, mid, state, roster):
    with _cache_lock:
        hit = _premade_cache                  # kam evtl. gerade erst herein
        if hit["match"] == key and hit["data"] and (time.time() - hit["ts"] < PREMADE_TTL or _match_current(hit["ts"])):
            return hit["data"]
    with _pd_count_lock:
        _pd_party["n"] += 1
    try:
        data = _premades_build(mid, state, roster)
    finally:
        with _pd_count_lock:
            _pd_party["n"] -= 1
    if pd_gate_state()["paused"]:
        # Historie unvollstaendig - nicht fuers ganze Match merken; die Seite fragt in 8 s erneut
        return {"ok": False, "error": "riot-ratelimit"}
    with _cache_lock:
        _premade_cache.update({"match": key, "ts": time.time(), "data": data})
    return data


# ---------------------------- RR-Verlauf ----------------------------

def current_match_id():
    """ID des laufenden Matches bzw. der Agentenauswahl — Schluessel fuer die
    Begegnungs-Zaehlung, damit dasselbe Match nur einmal zaehlt."""
    auth = get_auth()
    status, j = riot_get("glz", "/core-game/v1/players/%s" % auth["puuid"])
    if status == 200 and j and j.get("MatchID"):
        return {"ok": True, "matchId": j["MatchID"], "state": "INGAME"}
    mid = pregame_match_id()
    if mid:
        return {"ok": True, "matchId": mid, "state": "PREGAME"}
    return {"ok": False, "error": "Gerade kein laufendes Match."}


_rr_cache = {}          # "puuid:n" -> (ts, result)
RR_TTL = 180


_PUUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_puuid(value):
    return bool(value) and bool(_PUUID_RE.match(value))


def rr_history(puuid, count=15):
    """RR-Verlauf fuer die Kurve im Spieler-Fenster.

    Gecacht und mit einem zweiten Versuch abgesichert: Riot antwortet auf diesen
    Endpunkt sporadisch mit 4xx, und ein leeres Ergebnis liess die Kurve frueher
    mal erscheinen und mal nicht.
    """
    _snapshot_wait()
    key = "%s:%d" % (puuid, count)
    now = time.time()
    with _cache_lock:
        hit = _rr_cache.get(key)
        if hit and now - hit[0] < RR_TTL:
            return hit[1]

    path = ("/mmr/v1/players/%s/competitiveupdates?startIndex=0&endIndex=%d&queue=competitive"
            % (puuid, count))
    status, j = riot_get("pd", path)
    if status != 200 or not j:
        time.sleep(0.4)
        status, j = riot_get("pd", path)
    if status != 200 or not j:
        return {"ok": False, "error": "RR-Verlauf nicht lesbar (Status %s)." % status}
    points = []
    for m in (j.get("Matches") or []):
        # Beide Werte müssen da sein — sonst entsteht in der Kurve ein
        # Loch (NaN), und der Browser zeichnet die Linie gar nicht erst.
        if m.get("TierAfterUpdate") is None or m.get("RankedRatingAfterUpdate") is None:
            continue
        points.append({
            "map": m.get("MapID"),
            "at": m.get("MatchStartTime"),
            "earned": m.get("RankedRatingEarned"),
            "tier": m.get("TierAfterUpdate"),
            "rr": m.get("RankedRatingAfterUpdate"),
        })
    points.reverse()      # aeltestes zuerst
    out = {"ok": True, "points": points}
    with _cache_lock:
        _cache_put(_rr_cache, key, (now, out))
    return out


_self_name = {"puuid": None, "name": None}


def read_presets():
    """Datei-Inhalt: {puuid: {presetname: loadout}} — je Account getrennt."""
    try:
        # utf-8-sig: verkraftet auch eine Datei mit BOM (z.B. von Hand mit einem
        # Windows-Editor gespeichert) — sonst läge sie für uns "leer" vor.
        with open(PRESETS_FILE, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # Kaputte Datei nicht überschreiben, sondern beiseitelegen
        try:
            os.replace(PRESETS_FILE, PRESETS_FILE + ".broken")
        except Exception:
            pass
        return {}


def _split_legacy(all_p, puuid):
    """Alte Datei war flach ({name: loadout}) — solche Eintraege wandern unter den
    aktuellen Account, damit nichts verloren geht."""
    legacy = {k: v for k, v in all_p.items()
              if isinstance(v, dict) and v.get("Guns")}
    if not legacy:
        return all_p, False
    rest = {k: v for k, v in all_p.items() if k not in legacy}
    bucket = rest.setdefault(puuid, {})
    for k, v in legacy.items():
        bucket.setdefault(k, v)
    return rest, True


def account_presets():
    """Presets des gerade eingeloggten Accounts + dessen Anzeigename."""
    auth = get_auth()
    puuid = auth["puuid"]
    with _presets_lock:
        all_p = read_presets()
        all_p, changed = _split_legacy(all_p, puuid)
        if changed:
            write_presets(all_p)
        mine = all_p.get(puuid) or {}

    if _self_name["puuid"] != puuid:
        try:
            _self_name["name"] = names_for([puuid]).get(puuid)
        except Exception:
            _self_name["name"] = None
        _self_name["puuid"] = puuid

    return {"ok": True, "presets": mine, "file": os.path.basename(PRESETS_FILE),
            "account": _self_name["name"], "puuid": puuid}


def write_presets(data):
    """Atomar schreiben, damit die Datei bei einem Absturz nicht halb beschrieben ist."""
    tmp = PRESETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, PRESETS_FILE)


def _mutate_presets(fn):
    """Aendert die Presets des aktuellen Accounts unter Sperre."""
    puuid = get_auth()["puuid"]
    with _presets_lock:
        all_p = read_presets()
        all_p, _ = _split_legacy(all_p, puuid)
        bucket = all_p.setdefault(puuid, {})
        fn(bucket)
        write_presets(all_p)
        return dict(bucket)


def preset_save(name, data):
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Kein Name angegeben."}
    if not isinstance(data, dict) or not data.get("Guns"):
        return {"ok": False, "error": "Kein gueltiges Loadout uebergeben."}
    # Presets speichern ausschliesslich Waffen-Skins. Sprays, Spielerkarte und
    # Titel bleiben davon unberührt und werden nie mitgesetzt.
    slim = {"Guns": data["Guns"]}
    mine = _mutate_presets(lambda b: b.__setitem__(name, slim))
    return {"ok": True, "presets": mine, "file": os.path.basename(PRESETS_FILE)}


def preset_delete(name):
    mine = _mutate_presets(lambda b: b.pop(name, None))
    return {"ok": True, "presets": mine}


def preset_import(presets):
    """Einmalige Uebernahme alter Browser-Presets in den aktuellen Account."""
    if not isinstance(presets, dict) or not presets:
        return account_presets()

    def add(bucket):
        for k, v in presets.items():
            if k not in bucket and isinstance(v, dict) and v.get("Guns"):
                bucket[k] = v
    mine = _mutate_presets(add)
    return {"ok": True, "presets": mine}


SHOP_MAX_AGE = 6 * 3600      # Notbremse, falls Riot keine Restzeit mitschickt
SHOP_EDGE = 3                # so kurz vor dem Wechsel lieber frisch holen


def _shop_aged(data, age):
    """Gecachten Shop mit heruntergezaehlten Restzeiten ausliefern."""
    def left(v):
        return max(0, int(v - age)) if isinstance(v, (int, float)) else v

    out = dict(data)
    out["remaining"] = left(out.get("remaining"))
    if out.get("night"):
        night = dict(out["night"])
        night["remaining"] = left(night.get("remaining"))
        out["night"] = night
    out["bundles"] = [dict(b, remaining=left(b.get("remaining")))
                      for b in (out.get("bundles") or [])]
    out["cached"] = True
    out["age"] = int(age)
    return out


def _shop_expired(data, age):
    """Ist der gecachte Stand ueberholt (Angebote gewechselt)?"""
    if age > SHOP_MAX_AGE:
        return True
    rem = data.get("remaining")
    if isinstance(rem, (int, float)) and rem - age <= SHOP_EDGE:
        return True
    night = data.get("night") or {}
    nrem = night.get("remaining")
    if isinstance(nrem, (int, float)) and 0 < nrem - age <= SHOP_EDGE:
        return True
    for b in (data.get("bundles") or []):
        brem = b.get("remaining")
        if isinstance(brem, (int, float)) and 0 < brem - age <= SHOP_EDGE:
            return True
    return False


def get_shop(force=False):
    """Shop mit Cache — die Angebote aendern sich nur beim Tageswechsel.

    Ohne Cache lud der Shop-Tab bei jedem Aufruf komplett neu (mehrere
    Riot-Anfragen, sichtbares Nachladen). Jetzt kommt der gespeicherte Stand
    sofort zurueck, mit heruntergezaehlter Restzeit — und erst wenn die
    Restzeit abgelaufen ist (oder jemand ausdruecklich neu laedt), fragen wir
    Riot wieder.
    """
    now = time.time()
    puuid = get_auth()["puuid"]
    ts, cached = acct_cached(puuid, "shop")     # je Account, auch ueber einen Neustart
    if not force and cached:
        age = now - ts
        if not _shop_expired(cached, age):
            return _shop_aged(cached, age)

    fresh = _fetch_shop()
    if fresh.get("ok"):
        acct_store(puuid, "shop", fresh)
        return dict(fresh, cached=False, age=0)
    if cached:      # Riot zickt -> lieber der alte Stand als eine Fehlermeldung
        aged = _shop_aged(cached, now - ts)
        aged["stale"] = True
        return aged
    return fresh


def _fetch_shop():
    auth = get_auth()
    status, st = riot_post("pd", "/store/v3/storefront/%s" % auth["puuid"], "{}")
    if status != 200 or not st:
        return {"ok": False, "error": "Shop nicht lesbar (Status %s)." % status}
    _, wallet = riot_get("pd", "/store/v1/wallet/%s" % auth["puuid"])

    panel = st.get("SkinsPanelLayout") or {}
    prices = {}
    for o in (panel.get("SingleItemStoreOffers") or []):
        cost = o.get("Cost") or {}
        cur, val = (list(cost.items()) + [(None, None)])[0]
        prices[o.get("OfferID")] = {"cost": val, "currency": cur}

    offers = []
    for uid in (panel.get("SingleItemOffers") or []):
        p = prices.get(uid) or {}
        offers.append({"skinLevelId": uid, "cost": p.get("cost"), "currency": p.get("currency")})

    bundles = []
    for b in ((st.get("FeaturedBundle") or {}).get("Bundles") or []):
        items = []
        for it in (b.get("Items") or []):
            info = it.get("Item") or {}
            items.append({
                "typeId": info.get("ItemTypeID"),
                "itemId": info.get("ItemID"),
                "amount": info.get("Amount"),
                "base": it.get("BasePrice"),
                "price": it.get("DiscountedPrice"),
                "discount": it.get("DiscountPercent"),
                "promo": it.get("IsPromoItem"),
            })
        bundles.append({
            "itemList": items,
            "id": b.get("DataAssetID"),
            "baseCost": b.get("TotalBaseCost", {}).get(
                "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741") if isinstance(b.get("TotalBaseCost"), dict) else None,
            "cost": b.get("TotalDiscountedCost", {}).get(
                "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741") if isinstance(b.get("TotalDiscountedCost"), dict) else None,
            "discount": b.get("TotalDiscountPercent"),
            "remaining": b.get("DurationRemainingInSeconds"),
            "items": len(b.get("Items") or []),
        })

    night = st.get("BonusStore") or None
    night_out = None
    if night:
        offers_out = []
        for o in (night.get("BonusStoreOffers") or []):
            offer = o.get("Offer") or {}
            rewards = offer.get("Rewards") or []
            # Die eigentliche Item-ID steckt in den Rewards; OfferID nur als Rückfall
            item_id = rewards[0].get("ItemID") if rewards else offer.get("OfferID")
            type_id = rewards[0].get("ItemTypeID") if rewards else None
            base = list((offer.get("Cost") or {}).values()) or [None]
            disc = list((o.get("DiscountCosts") or {}).values()) or [None]
            offers_out.append({
                "skinLevelId": item_id,
                "typeId": type_id,
                "discount": o.get("DiscountPercent"),
                "base": base[0],
                "cost": disc[0],
                "seen": o.get("IsSeen"),
            })
        night_out = {
            "remaining": night.get("BonusStoreRemainingDurationInSeconds"),
            "offers": offers_out,
        }

    return {
        "ok": True,
        "offers": offers,
        "remaining": panel.get("SingleItemOffersRemainingDurationInSeconds"),
        "bundles": bundles,
        "night": night_out,
        "wallet": (wallet or {}).get("Balances") or {},
    }


_pregame_mid = {"id": None, "ts": 0.0}
# Seit wann kennt der Dienst welche Agentenauswahl? Daran haengt die Schutzzeit fuers
# Waehlen von Hand (MANUAL_LOCK_MIN beim Instalock). Jede frische MatchID laeuft
# ueber _pregame_note() - wer eine neue Stelle baut, muss sie auch dort durchschicken.
_pregame_seen = collections.OrderedDict()
_pregame_seen_lock = threading.Lock()


def _pregame_note(mid):
    """Frische MatchID der Agentenauswahl merken (Cache + wann zuerst gesehen)."""
    now = time.time()
    _pregame_mid["id"] = mid
    _pregame_mid["ts"] = now
    with _pregame_seen_lock:
        if mid not in _pregame_seen:
            _pregame_seen[mid] = now
            while len(_pregame_seen) > 8:
                _pregame_seen.popitem(last=False)


def manual_lock_wait(mid):
    """Sekunden, bis RankYoinker in dieser Agentenauswahl von Hand waehlen/sperren darf.
    Unbekannte Auswahl (gerade erst aufgetaucht) -> volle Schutzzeit."""
    with _pregame_seen_lock:
        seen = _pregame_seen.get(mid)
    if seen is None:
        return MANUAL_LOCK_MIN
    return max(0.0, seen + MANUAL_LOCK_MIN - time.time())


def pregame_match_id(force=False):
    """MatchID der laufenden Agentenauswahl — kurz gecacht, damit das
    3-Sekunden-Polling nicht jedes Mal zwei Riot-Anfragen braucht."""
    if not force and _pregame_mid["id"] and time.time() - _pregame_mid["ts"] < 60:
        return _pregame_mid["id"]
    auth = get_auth()
    status, j = riot_get("glz", "/pregame/v1/players/%s" % auth["puuid"])
    if status != 200 or not j or not j.get("MatchID"):
        _pregame_mid["id"] = None
        return None
    _pregame_note(j["MatchID"])
    return _pregame_mid["id"]


_pregame_cache = {"ts": 0.0, "data": None, "gen": 0}
_pregame_lock = threading.Lock()
PREGAME_TTL = 0.7   # Sekunden: die Seite darf oefter fragen als wir Riot fragen


def pregame_state():
    """Wer hat welchen Agenten gewaehlt bzw. schwebt nur darueber?

    Kurz gecacht, damit ein schnelles Polling im Browser nicht 1:1 auf Riot
    durchschlaegt — mehrere Abfragen pro Sekunde teilen sich eine Antwort.
    Immer nur eine Abfrage gleichzeitig (2.4.8.2): Dauerte eine laenger als der
    1-s-Takt der Seite, liefen vorher mehrere parallel und schrieben sich
    gegenseitig ueber. Wer waehrenddessen fragt, wartet und bekommt den Cache.
    Eine Abfrage, die vor einem eigenen Hover/Lock gestartet ist ("gen"), kommt
    nicht in den Cache - sonst sah die Seite danach noch kurz den alten Agenten.
    """
    with _pregame_lock:
        now = time.time()
        if _pregame_cache["data"] and now - _pregame_cache["ts"] < PREGAME_TTL:
            return _pregame_cache["data"]
        gen = _pregame_cache["gen"]
        result = _pregame_fetch()
        if _pregame_cache["gen"] == gen:
            _pregame_cache["data"] = result
            _pregame_cache["ts"] = time.time()
        return result


def _pregame_invalidate():
    """Eigene Wahl sofort sichtbar machen: Cache leeren und laufende Abfragen entwerten."""
    _pregame_cache["gen"] += 1
    _pregame_cache["ts"] = 0.0


def _pregame_fetch():
    mid = pregame_match_id()
    if not mid:
        return {"ok": False, "error": "Gerade keine Agentenauswahl aktiv."}
    status, m = riot_get("glz", "/pregame/v1/matches/%s" % mid)
    if status != 200 or not m:
        mid = pregame_match_id(force=True)      # Auswahl gewechselt -> neu aufloesen
        if not mid:
            return {"ok": False, "error": "Agentenauswahl beendet."}
        status, m = riot_get("glz", "/pregame/v1/matches/%s" % mid)
        if status != 200 or not m:
            return {"ok": False, "error": "Agentenauswahl nicht lesbar (Status %s)." % status}

    raw = (m.get("AllyTeam") or {}).get("Players") or []
    ally_puuids = [p.get("Subject") for p in raw if p.get("Subject")]
    name_map = names_for_nonblocking(ally_puuids)
    loadouts_by_puuid = _pregame_loadouts_by_puuid(mid)
    players = []
    for p in raw:
        sel = p.get("CharacterSelectionState") or ""
        ident = p.get("PlayerIdentity") or {}
        subj = p.get("Subject")
        lo = (loadouts_by_puuid or {}).get(subj) or {}
        players.append({
            "puuid": subj,
            "name": name_map.get(subj) or None,
            "level": ident.get("AccountLevel"),
            "card": ident.get("PlayerCardID"),
            "agent": p.get("CharacterID") or None,
            # "" = nichts, "selected" = hovert nur, "locked" = fest gewählt
            "locked": sel == "locked",
            "hovering": sel == "selected",
            "weapons": lo.get("weapons"),
        })

    enemies = _pregame_loadouts_enemies(mid, ally_puuids, loadouts_by_puuid)
    if enemies is None:
        enemies = _pregame_enemies_best_effort(m, ally_puuids)
    # Eigene Seite: Riot nennt die Teams "Red" (greift in der ersten Hälfte an)
    # und "Blue" (verteidigt). Nach der Halbzeit wird getauscht.
    team = str((m.get("AllyTeam") or {}).get("TeamID") or "")
    side = {"red": "attack", "blue": "defense"}.get(team.lower())
    # Karte als Riot-Pfad ("/game/maps/jam/jam"): vry.exe schickt in der Agentenauswahl
    # keine, das Kopfbild blieb dort leer
    return {"ok": True, "matchId": mid, "phase": m.get("Phase"),
            "timeLeft": m.get("PhaseTimeRemainingNS"), "players": players,
            "enemies": enemies, "side": side, "map": (m.get("MapID") or "").lower() or None}


UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _jwt_payload(token):
    """JWT-Payload ohne Signaturpruefung dekodieren (nur zum Auslesen, kein Auth)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None


def _collect_strings(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_strings(v, out)


# Feste Socket-Uuids fuer Skin/Chroma/Buddy in einem Waffen-Loadout-Eintrag -
# identisch zu setup/build_source/src/constants.py "sockets" (dort fuer den
# INGAME-Pfad in vry.exe), hier nochmal fuer die Agentenauswahl gebraucht.
SOCKET_SKIN = "bcef87d6-209b-46c6-8b19-fbe40bd95abc"
SOCKET_SKIN_CHROMA = "3ad1b2b2-acdb-4524-852f-954a76ddae0a"

_weapons_cache = {"ts": 0.0, "data": None}
WEAPONS_TTL = 21600   # 6h - Waffen/Skins aendern sich praktisch nie zur Laufzeit

# Warden (13.06-Patch, 2026-09-22/23) fehlt bei valorant-api.com noch (Community-
# Projekt, haengt bei ganz neuen Inhalten ein paar Tage hinterher - dasselbe
# Muster wie zuvor bei Maps/Spielmodus/Fraktionen). IDs aus einem echten Live-
# Loadout (Community, 2026-09-23); Standard-Skin ist bislang die einzige
# bestaetigte Option - zwei unabhaengige Signale stimmen ueberein (weder in
# den Skin-Entitlements eines Accounts mit hunderten Skins sonst, noch auf der
# offiziellen Waffenseite taucht ein Kauf-Skin fuer Warden auf). Icon offiziell
# bei Riot selbst gehostet. Siehe auch die JS-Entsprechung addWardenFallback()
# in index.html - beide Stellen lesen von valorant-api.com und brauchen daher
# dieselbe Ergaenzung, bis die Waffe dort selbst gelistet wird.
WARDEN_UUID = "8db0a1bf-4a50-832a-4566-faaaa6d250ca"
WARDEN_STANDARD_SKIN = "61d99a36-4033-0fa9-2c94-71aaf901e120"
WARDEN_STANDARD_LEVEL = "45a2214f-4730-8c89-b0a4-a7a2b5125ac8"
WARDEN_ICON = "https://wiki.playvalorant.com/en-us/images/thumb/Warden.png/512px-Warden.png"


def _with_warden_fallback(weapons):
    if any((w.get("uuid") or "").lower() == WARDEN_UUID for w in weapons):
        return weapons   # valorant-api.com hat nachgezogen - nichts zu tun
    return weapons + [{
        "uuid": WARDEN_UUID, "displayName": "Warden", "displayIcon": WARDEN_ICON,
        "skins": [{
            "uuid": WARDEN_STANDARD_SKIN, "displayName": "Standard Warden",
            "displayIcon": WARDEN_ICON, "chromas": [],
            "levels": [{"uuid": WARDEN_STANDARD_LEVEL, "displayIcon": WARDEN_ICON}],
        }],
    }]


def _valo_weapons():
    now = time.time()
    if _weapons_cache["data"] and now - _weapons_cache["ts"] < WEAPONS_TTL:
        return _weapons_cache["data"]
    try:
        _, j = _http("https://valorant-api.com/v1/weapons")
        data = (j or {}).get("data") or []
        if data:
            data = _with_warden_fallback(data)
            _weapons_cache["data"] = data
            _weapons_cache["ts"] = now
        return data or _weapons_cache["data"] or []
    except Exception:
        return _weapons_cache["data"] or []


def _parse_pregame_weapons(loadout_obj):
    """Ein einzelnes Loadout (aus /pregame/v1/matches/{id}/loadouts) in die
    gleiche Form bringen, die der Client schon von INGAME kennt (siehe
    convertLoadoutToJsonArray in setup/build_source/src/Loadouts.py):
    {skin_item_uuid: {weapon, skinDisplayName, skinDisplayIcon}}. primarySkin()
    im Frontend pickt sich daraus selbst die konfigurierte Hauptwaffe raus,
    hier wird bewusst alles mitgeschickt.

    Die genaue Form eines Pregame-Loadout-Eintrags ist nirgends dokumentiert
    (der INGAME-Pfad in main.py nutzt "Loadout" als Zwischenschluessel, ob das
    fuer die Agentenauswahl genauso ist, war vorher nie getestet - siehe der
    lange auskommentierte Aufruf dort). Darum hier beides versuchen und bei
    jedem unerwarteten Fehler einfach nichts liefern statt das ganze Polling
    mitzureissen - ein fehlender Skin ist unschoen, ein kaputtes Polling waere
    schlimmer.
    """
    try:
        items = (loadout_obj or {}).get("Items")
        if not isinstance(items, dict):
            items = ((loadout_obj or {}).get("Loadout") or {}).get("Items")
        if not isinstance(items, dict):
            return None
        weapons = _valo_weapons()
        if not weapons:
            return None
        by_uuid = {w["uuid"].lower(): w for w in weapons if w.get("uuid")}
        out = {}
        for weapon_uuid, item in items.items():
            weapon = by_uuid.get(str(weapon_uuid).lower())
            if not weapon:
                continue
            sockets_data = (item or {}).get("Sockets") or {}
            skin_id = ((sockets_data.get(SOCKET_SKIN) or {}).get("Item") or {}).get("ID")
            if not skin_id:
                continue
            skin = next((s for s in (weapon.get("skins") or [])
                         if (s.get("uuid") or "").lower() == str(skin_id).lower()), None)
            if not skin:
                continue
            weapon_name = weapon.get("displayName") or ""
            entry = {
                "weapon": weapon_name,
                "skinDisplayName": (skin.get("displayName") or "").replace(
                    " " + weapon_name, ""),
            }
            chroma_id = ((sockets_data.get(SOCKET_SKIN_CHROMA) or {}).get("Item") or {}).get("ID")
            chroma = next((c for c in (skin.get("chromas") or [])
                           if (c.get("uuid") or "").lower() == str(chroma_id or "").lower()), None)
            icon = ((chroma or {}).get("displayIcon") or (chroma or {}).get("fullRender")
                    or skin.get("displayIcon")
                    or ((skin.get("levels") or [{}])[0]).get("displayIcon"))
            if (skin.get("displayName") or "").startswith(("Standard", "Melee")):
                icon = weapon.get("displayIcon")
            if icon:
                entry["skinDisplayIcon"] = icon
            out[str(skin_id)] = entry
        return out or None
    except Exception:
        return None


_pregame_loadouts_cache = {"mid": None, "ts": 0.0, "data": None}
PREGAME_LOADOUTS_TTL = 1.5   # etwas grosszuegiger als PREGAME_TTL - deutlich teurer pro Aufruf


def _pregame_loadouts_by_puuid(mid):
    """Ein Aufruf von /pregame/v1/matches/{id}/loadouts deckt BEIDE Teams ab -
    liefert je Spieler-Puuid {"agent": <uuid-oder-None>, "weapons": <dict-oder-None>}.

    Dieser Endpunkt existiert, damit der offizielle Client schon waehrend der
    Agentenauswahl weiss, welche Waffen-Skins spaeter im Killfeed auftauchen -
    dafuer braucht er die Loadouts BEIDER Teams, nicht nur der eigenen Seite.
    Im Gegensatz zum Haupt-Match-Endpunkt (der Gegner-Puuids in normalen
    Matches bewusst zurueckhaelt) gibt Riot hier zuverlaessig alle zehn
    Spieler mit "Subject" (Puuid) und "CharacterID" (gewaehlter Agent) heraus.
    Ein Tipp aus der Community, danke dafuer.

    Kurz gecacht, weil sowohl /api/pregame (1s-Polling) als auch /api/premades
    ihn kurz hintereinander brauchen koennen. None bei jedem Fehler, damit
    Aufrufer sauber auf ihre jeweilige Bestenfalls-Methode zurueckfallen.
    """
    now = time.time()
    if (_pregame_loadouts_cache["mid"] == mid and _pregame_loadouts_cache["data"] is not None
            and now - _pregame_loadouts_cache["ts"] < PREGAME_LOADOUTS_TTL):
        return _pregame_loadouts_cache["data"]
    try:
        status, j = riot_get("glz", "/pregame/v1/matches/%s/loadouts" % mid)
        if status != 200 or not isinstance(j, dict):
            out = None
        else:
            out = {}
            for entry in (j.get("Loadouts") or []):
                subj = entry.get("Subject")
                if subj:
                    out[subj] = {
                        "agent": entry.get("CharacterID") or None,
                        "weapons": _parse_pregame_weapons(entry),
                    }
    except Exception:
        out = None
    _pregame_loadouts_cache.update({"mid": mid, "ts": now, "data": out})
    return out


def _pregame_loadouts_enemies(mid, ally_puuids, loadouts_by_puuid=None):
    """Gegnerliste (Rang+Agent+Skins) ueber _pregame_loadouts_by_puuid.

    Gibt bei Erfolg eine (moeglicherweise leere) Liste zurueck, bei jedem
    Fehler None - der Aufrufer faellt dann auf die bisherige Bestenfalls-
    Methode zurueck, statt den ganzen Aufruf scheitern zu lassen.
    """
    try:
        if loadouts_by_puuid is None:
            loadouts_by_puuid = _pregame_loadouts_by_puuid(mid)
        if loadouts_by_puuid is None:
            return None

        skip = set(ally_puuids) | {get_auth()["puuid"]}
        candidates = [u for u in loadouts_by_puuid if u not in skip]
        if not candidates:
            return []

        name_map = names_for_nonblocking(candidates)
        enemy_puuids = [u for u in candidates if name_map.get(u)]
        if not enemy_puuids:
            return []

        # nicht auf pd warten (siehe names_for) - Raenge kommen im naechsten Poll
        ranks = {u: mmr_summary_nonblocking(u) for u in enemy_puuids}
        # Community-Idee: meistgespielter Agent der Gegner schon in der
        # Agentenauswahl. Bewusst NICHT blockierend (siehe
        # most_played_agent_nonblocking) - das koennte pro Spieler bis zu vier
        # zusaetzliche Riot-Aufrufe brauchen, und /api/pregame wird jede
        # Sekunde neu abgefragt; das soll dadurch nicht spuerbar langsamer
        # werden. Taucht einfach im naechsten Poll auf, sobald es da ist.
        history = {u: most_played_agent_nonblocking(u) for u in enemy_puuids}
        return [{
            "puuid": u,
            "name": name_map.get(u),
            "agent": loadouts_by_puuid[u].get("agent"),
            "weapons": loadouts_by_puuid[u].get("weapons"),
            "rank": ranks[u].get("rank"),
            "rr": ranks[u].get("rr"),
            "peakRank": ranks[u].get("peakRank"),
            "peakRankAct": ranks[u].get("peakRankAct"),
            "mostPlayedAgent": (history.get(u) or {}).get("agent"),
            "mostPlayedAgentCount": (history.get(u) or {}).get("count"),
            "mostPlayedAgentGames": (history.get(u) or {}).get("games"),
        } for u in enemy_puuids]
    except Exception:
        return None


def _pregame_enemies_best_effort(match_json, ally_puuids):
    """Gegner-Raenge in der Agentenauswahl -- nur ein Versuch, kein Verlass.

    Riot schickt AllyTeam/EnemyTeam waehrend der Auswahl bewusst so, dass
    normale Ranked-/Unrated-Spiele keine Gegner-Puuids preisgeben (Schutz vor
    Dodging nach Rang). Trotzdem gibt es Sonderfaelle (z. B. Custom-Games mit
    bekannten Teilnehmern), in denen Riot mehr in der Antwort mitschickt, als
    die offiziell dokumentierten Felder ("EnemyTeam", "TeamMatchToken") nahe-
    legen. Statt uns auf einen exakten Feldnamen zu verlassen (der sich schon
    mehrfach geaendert hat, siehe Patch-Hinweise weiter oben), sammeln wir
    JEDEN String in der Antwort ein, dekodieren darin gefundene JWTs mit und
    filtern am Ende auf alles, was wie eine Puuid aussieht. Ergebnis wird ueber
    den Namensdienst geprueft: Match-/Party-/Queue-IDs sind zufaellig auch
    UUID-foermig, aber nur echte Accounts bekommen einen Namen zurueck.
    """
    try:
        strings = []
        _collect_strings(match_json, strings)
        for s in list(strings):
            if s.count(".") == 2 and len(s) > 20:
                payload = _jwt_payload(s)
                if payload:
                    _collect_strings(payload, strings)

        found = set()
        for s in strings:
            found.update(UUID_RE.findall(s))

        skip = set(ally_puuids) | {get_auth()["puuid"]}
        candidates = [u for u in found if u not in skip]
        if not candidates:
            return []

        name_map = names_for_nonblocking(candidates)
        enemy_puuids = [u for u in candidates if name_map.get(u)][:5]
        if not enemy_puuids:
            return []

        ranks = {u: mmr_summary_nonblocking(u) for u in enemy_puuids}
        return [{
            "puuid": u,
            "name": name_map.get(u),
            "rank": ranks[u].get("rank"),
            "rr": ranks[u].get("rr"),
            "peakRank": ranks[u].get("peakRank"),
            "peakRankAct": ranks[u].get("peakRankAct"),
        } for u in enemy_puuids]
    except Exception:
        return []


def dodge_pregame():
    """Verlaesst die laufende Agentenauswahl -> Queue wird gedodged."""
    auth = get_auth()
    status, j = riot_get("glz", "/pregame/v1/players/%s" % auth["puuid"])
    if status != 200 or not j:
        return {"ok": False, "error": "Gerade keine Agentenauswahl aktiv (Status %s)." % status}
    match_id = j.get("MatchID")
    if not match_id:
        return {"ok": False, "error": "Keine Match-ID in der Agentenauswahl gefunden."}
    status, _ = riot_post("glz", "/pregame/v1/matches/%s/quit" % match_id)
    if status in (200, 204):
        record_feature("dodge")
        return {"ok": True, "matchId": match_id}
    return {"ok": False, "error": "Verlassen fehlgeschlagen (Status %s)." % status}


# ============================ Instalock ============================
# Früher lief der Instalock komplett im Browser (setInterval + setTimeout).
# Sobald der Tab in den Hintergrund rutschte, drosselte der Browser beide
# Timer auf etwa einen Aufruf pro Minute — aus 7 Sekunden Verzögerung wurden
# so schnell 50, und mit geschlossenem Tab passierte gar nichts mehr.
# Darum tickt die Uhr jetzt hier: Der Dienst läuft unabhängig vom Browser,
# die Seite zeigt nur noch an, was der Dienst ohnehin tut.

IL_TICK = 0.2             # Takt des Waechters (Genauigkeit des Ausloesens)
IL_POLL = 0.6             # so oft wird auf eine neue Agentenauswahl geprueft
IL_LOCK_TRIES = 10        # ganz zu Beginn der Auswahl lehnt Riot gerne mal ab
IL_LOCK_GAP = 0.35

# Fest vorgegeben, NICHT vom Client uebernehmbar (siehe instalock_arm unten) -
# genau diese zufaellige 11-12s-Verzoegerung ist die ganze Begruendung dafuer,
# dass Instalock kein verbotenes Instant-Lock-Tool ist (siehe die Risiko-
# Bestaetigung in index.html). Wuerde der Server einen vom Client geschickten
# delay/jitter-Wert uebernehmen, koennte jeder ueber ein manipuliertes
# index.html oder eine direkte Anfrage an /api/instalock/arm auf 0s stellen.
IL_DELAY = 11.5
IL_JITTER = 0.5
# Dieselbe Schutzzeit gilt fuers Hovern und Sperren von Hand ueber RankYoinker
# (Agentenauswahl im Overlay, auch am Handy) - auch ohne scharfen Instalock. Gezaehlt ab dem
# ersten Sehen der Auswahl (_pregame_note), geprueft im Dienst (agent_action).
MANUAL_LOCK_MIN = IL_DELAY - IL_JITTER
MANUAL_LOCK_GRACE = 1.0   # so knapp zu frueh wartet der Dienst einfach kurz

_il_lock = threading.Lock()
_il = {
    "on": False,          # scharf?
    "agent": None,        # Agenten-UUID
    "name": "",           # Anzeigename (nur fuer Meldungen)
    "delay": IL_DELAY,    # Verzoegerung in Sekunden (fest, siehe oben)
    "jitter": IL_JITTER,  # zufaellige Streuung +/- Sekunden (fest, siehe oben)
    "match": None,        # Agentenauswahl, auf die gefeuert wird
    "fireAt": 0.0,        # Zeitpunkt des Sperrens
    "planned": 0.0,       # tatsaechlich gewuerfelte Verzoegerung
    "status": "off",      # off | armed | waiting | locked | done | error
    "message": "",
    "at": 0.0,            # wann zuletzt gefeuert wurde
    "lockedAt": 0.0,       # wann zuletzt gesperrt wurde (fuer den Timeout in Phase 3)
    "free": 1,            # so viele Matches noch ohne Nachfrage, das naechste eingeschlossen
    "ingameMatch": None,  # Match, in dem zuletzt gesperrt wurde (Phase 4)
    "ingameAt": 0.0,
    "askAt": 0.0,         # seit wann die Frage nach dem Match offen ist (Phase 5)
    "asked": False,       # Fenster ueber VALORANT fuer diese Frage schon gestartet?
    "gen": 0,             # zaehlt jede Zustandsaenderung (fuer die Oberflaeche)
}
_il_poll = {"ts": 0.0}

# Nach dem Sperren ist noch nicht sicher, dass daraus ein echtes Spiel wird —
# wird das Match gedodged, landet man in einer neuen Agentenauswahl. Darum
# bleibt der Instalock nach dem Sperren scharf und feuert im Dodge-Fall erneut.
# Sicherheitsnetz, falls das echte Spiel nie erkannt wird (API-Aussetzer o.ä.):
IL_LOCKED_TIMEOUT = 180.0

# ---- Instalock bleibt aktiv (2.5.5, Nutzerwunsch) ----
# Nach einem Match, in dem er gesperrt hat, bleibt der Instalock an und fragt: ein
# kleines Fenster ueber VALORANT (il_popup.py, eigener Prozess) und dieselbe Frage im
# Overlay - "Weiter mit Jett?" Ja / Nein, dazu "die naechsten 3 Matches nicht fragen".
# Ohne Antwort bis zur naechsten Agentenauswahl pausiert er (Wunsch des Nutzers):
# gesperrt wird dann nicht, im Overlay wieder einschalten.
IL_FREE_YES = 1               # "Ja": das naechste Match
IL_FREE_SKIP = 3              # "Ja" + Haken: die naechsten 3 Matches ohne Nachfrage
IL_INGAME_MAX = 3 * 3600      # Sicherheitsnetz, falls das Matchende nie erkannt wird
IL_ASK_MAX = 15 * 60          # so lange bleibt die Frage hoechstens offen, dann Pause
IL_POPUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "il_popup.py")

# Der Instalock lag bisher nur im Arbeitsspeicher: Wird der Dienst beendet
# (Absturz, Update, Neustart), war das Scharfschalten still weg — man merkt es
# erst, wenn in der Agentenauswahl nichts passiert. Darum liegt der Stand jetzt
# auf der Platte und wird beim Start zurückgeholt.
INSTALOCK_FILE = os.path.join(DATA_DIR, "vry_instalock.json")
IL_RESUME_MAX = 600      # Sekunden: aelteres Scharfschalten NICHT wiederbeleben


def _il_save():
    """Aktuellen Stand sichern (ohne Sperre aufzurufen — Aufrufer haelt sie)."""
    try:
        _save_json(INSTALOCK_FILE, {
            "on": bool(_il["on"]), "agent": _il["agent"], "name": _il["name"],
            "delay": _il["delay"], "jitter": _il["jitter"], "savedAt": time.time(),
            "status": _il["status"], "free": _il["free"], "ingameMatch": _il["ingameMatch"],
            "ingameAt": _il["ingameAt"], "askAt": _il["askAt"],
        })
    except Exception:
        pass


def _il_restore():
    """Beim Dienststart: Agent/Verzoegerung uebernehmen, ein frisches
    Scharfschalten wieder aufnehmen.

    Bewusst mit Verfallszeit: Ein Instalock von gestern soll nicht Tage spaeter
    plotzlich einen Agenten sperren — nur ein Neustart mitten in der Sitzung
    darf ihn ueberleben.
    """
    data = _load_json(INSTALOCK_FILE)
    if not isinstance(data, dict) or not data.get("agent"):
        return
    now = time.time()
    on, st = bool(data.get("on")), data.get("status")
    upd = {
        "agent": data.get("agent"), "name": data.get("name") or "",
        "delay": IL_DELAY,
        "jitter": IL_JITTER,
        "on": False, "status": "off", "message": "",
        "match": None, "fireAt": 0.0, "planned": 0.0,
        "free": max(1, int(data.get("free") or 1)), "ingameMatch": None, "ingameAt": 0.0,
        "askAt": 0.0, "asked": False,
    }
    ingame_at, ask_at = float(data.get("ingameAt") or 0), float(data.get("askAt") or 0)
    if on and st == "ingame" and data.get("ingameMatch") and now - ingame_at < IL_INGAME_MAX:
        # Neustart mitten im Match (z.B. Update): nach dem Match trotzdem fragen
        upd.update({"on": True, "status": "ingame", "ingameMatch": data["ingameMatch"], "ingameAt": ingame_at})
    elif on and st == "ask" and now - ask_at < IL_ASK_MAX:
        upd.update({"on": True, "status": "ask", "askAt": ask_at})     # Fenster kommt im naechsten Takt wieder
    elif on and now - float(data.get("savedAt") or 0) < IL_RESUME_MAX:
        upd.update({"on": True, "status": "armed", "message": "Dienst neu gestartet — Instalock wieder scharf."})
    elif st == "paused":
        upd.update({"status": "paused", "message": "Instalock pausiert."})
    with _il_lock:
        _il.update(upd)


def _pregame_id_fresh():
    """MatchID der Agentenauswahl OHNE Cache.

    Der Instalock darf nie eine alte ID erwischen — sonst sperrt er beim
    naechsten Match in die vorige Auswahl hinein. Der gemeinsame Cache wird
    dabei gleich mit aktualisiert.
    """
    auth = get_auth()
    status, j = riot_get("glz", "/pregame/v1/players/%s" % auth["puuid"])
    if status != 200 or not j or not j.get("MatchID"):
        return None
    _pregame_note(j["MatchID"])
    return j["MatchID"]


def instalock_state():
    with _il_lock:
        remaining = (max(0.0, _il["fireAt"] - time.time())
                     if _il["status"] == "waiting" else 0.0)
        return {
            "ok": True, "on": bool(_il["on"]), "agent": _il["agent"],
            "name": _il["name"], "delay": _il["delay"], "jitter": _il["jitter"],
            "status": _il["status"], "message": _il["message"],
            "matchId": _il["match"], "remaining": round(remaining, 1),
            "planned": round(_il["planned"], 1), "at": int(_il["at"]),
            "free": _il["free"], "skipN": IL_FREE_SKIP,
            "askLeft": (int(max(0, _il["askAt"] + IL_ASK_MAX - time.time()))
                        if _il["status"] == "ask" else 0),
            "gen": _il["gen"],
        }


def instalock_arm(body):
    """Scharf schalten. Die Seite darf das jederzeit erneut schicken —
    zuletzt gewinnt, ohne dass eine laufende Verzoegerung durcheinanderkommt.

    delay/jitter kommen bewusst NICHT aus body - siehe IL_DELAY/IL_JITTER
    oben, fest im Dienst vorgegeben."""
    agent = (body or {}).get("agent")
    if not agent:
        return {"ok": False, "error": "Kein Agent angegeben."}
    with _il_lock:
        _il.update({
            "on": True, "agent": agent, "name": (body or {}).get("name") or "",
            "delay": IL_DELAY,
            "jitter": IL_JITTER,
            "match": None, "fireAt": 0.0, "planned": 0.0, "status": "armed",
            "lockedAt": 0.0, "free": IL_FREE_YES, "ingameMatch": None, "ingameAt": 0.0,
            "askAt": 0.0, "asked": False,
            "message": "Wartet auf die naechste Agentenauswahl.",
            "gen": _il["gen"] + 1,
        })
        _il_save()
    _il_popup_close()
    _il_poll["ts"] = 0.0        # sofort nachsehen, nicht erst im naechsten Takt
    record_feature("instalock_armed")
    return instalock_state()


def instalock_disarm():
    with _il_lock:
        _il.update({"on": False, "status": "off", "message": "", "match": None,
                    "fireAt": 0.0, "planned": 0.0, "ingameMatch": None, "askAt": 0.0,
                    "asked": False, "gen": _il["gen"] + 1})
        _il_save()
    _il_popup_close()
    return instalock_state()


def _il_pause(message):
    """Keine Antwort (oder Matchende nicht erkennbar): nicht sperren, bis man im
    Overlay wieder einschaltet. Agent bleibt gewaehlt."""
    with _il_lock:
        _il.update({"on": False, "status": "paused", "message": message, "match": None,
                    "fireAt": 0.0, "planned": 0.0, "ingameMatch": None, "askAt": 0.0,
                    "asked": False, "at": time.time(), "gen": _il["gen"] + 1})
        _il_save()
    _il_popup_close()


def instalock_answer(answer):
    """Antwort auf "Weiter mit <Agent>?" - aus dem Fenster ueber VALORANT oder dem Overlay.
    yes = naechstes Match, yes3 = die naechsten IL_FREE_SKIP Matches ohne Nachfrage, no = aus."""
    if answer not in ("yes", "yes3", "no"):
        return {"ok": False, "error": "Unbekannte Antwort."}
    with _il_lock:
        open_q = bool(_il["on"]) and _il["status"] == "ask"
        if open_q:
            if answer == "no":
                _il.update({"on": False, "status": "off", "message": "", "askAt": 0.0,
                            "asked": False, "gen": _il["gen"] + 1})
            else:
                _il.update({"status": "armed", "free": IL_FREE_SKIP if answer == "yes3" else IL_FREE_YES,
                            "match": None, "fireAt": 0.0, "planned": 0.0, "lockedAt": 0.0,
                            "askAt": 0.0, "asked": False,
                            "message": "Wartet auf die naechste Agentenauswahl.",
                            "gen": _il["gen"] + 1})
            _il_save()
    _il_popup_close()
    if not open_q:
        return dict(instalock_state(), ok=False, error="Gerade keine Frage offen.")
    _il_poll["ts"] = 0.0
    record_feature("instalock_keep_" + answer)
    return instalock_state()


# ---- Fenster ueber VALORANT (il_popup.py) ----
# Eigener Prozess: ein Fehler in den Win32-Aufrufen dort reisst den Dienst nicht mit.
# Antwort kommt als Exit-Code zurueck (siehe EXIT_* in il_popup.py). Klappt das Fenster
# nicht (exklusives Vollbild, Fehler), bleibt die Frage im Overlay - und ohne Antwort
# pausiert der Instalock ohnehin.
IL_POPUP_CFG = os.path.join(DATA_DIR, "il-popup.json")
IL_POPUP_ERR = os.path.join(DATA_DIR, "il-popup-error.txt")
_IL_POPUP_EXIT = {10: "yes", 13: "yes3", 20: "no", 30: None}
_il_popup = {"proc": None}
_il_popup_lock = threading.Lock()

_IL_POPUP_STRINGS = {
    "de": {"title": "Weiter mit {agent}?", "body": "Soll Instalock {agent} auch im nächsten Match locken? Ohne Antwort pausiert er.",
           "skip": "Die nächsten {n} Matches nicht fragen", "yes": "Ja", "no": "Nein"},
    "en": {"title": "Keep going with {agent}?", "body": "Should Instalock lock {agent} in the next match too? No answer pauses it.",
           "skip": "Don't ask for the next {n} matches", "yes": "Yes", "no": "No"},
    "pl": {"title": "Dalej {agent}?", "body": "Czy Instalock ma zablokować {agent} także w następnym meczu? Bez odpowiedzi zostanie wstrzymany.",
           "skip": "Nie pytaj przez następne {n} mecze", "yes": "Tak", "no": "Nie"},
    "fr": {"title": "Continuer avec {agent} ?", "body": "L'Instalock doit-il aussi verrouiller {agent} au prochain match ? Sans réponse, il se met en pause.",
           "skip": "Ne plus demander pendant {n} matchs", "yes": "Oui", "no": "Non"},
    "es": {"title": "¿Seguir con {agent}?", "body": "¿Instalock debe bloquear a {agent} también en la próxima partida? Sin respuesta, se pausa.",
           "skip": "No preguntar en las próximas {n} partidas", "yes": "Sí", "no": "No"},
    "tr": {"title": "{agent} ile devam mı?", "body": "Instalock bir sonraki maçta da {agent} kilitlesin mi? Cevap verilmezse duraklatılır.",
           "skip": "Sonraki {n} maçta sorma", "yes": "Evet", "no": "Hayır"},
    "ko": {"title": "{agent}(으)로 계속할까요?", "body": "다음 매치에서도 인스타락이 {agent}을(를) 고정할까요? 응답이 없으면 일시 중지됩니다.",
           "skip": "다음 {n}매치 동안 묻지 않기", "yes": "예", "no": "아니요"},
    "pt": {"title": "Continuar com {agent}?", "body": "O Instalock deve travar {agent} também na próxima partida? Sem resposta, ele pausa.",
           "skip": "Não perguntar nas próximas {n} partidas", "yes": "Sim", "no": "Não"},
    "ru": {"title": "Дальше на {agent}?", "body": "Instalock залочит {agent} и в следующем матче? Без ответа он встанет на паузу.",
           "skip": "Не спрашивать следующие {n} матча", "yes": "Да", "no": "Нет"},
    "ja": {"title": "{agent}で続けますか？", "body": "次のマッチでもインスタロックで{agent}をロックしますか？応答がない場合は一時停止します。",
           "skip": "次の{n}マッチは確認しない", "yes": "はい", "no": "いいえ"},
}
_IL_POPUP_FONTS = {"ko": "Malgun Gothic", "ja": "Yu Gothic UI"}


def _il_popup_open(agent_id, name, ask_at):
    """Fenster starten (nur Windows). Laeuft im eigenen Thread - das Agentenbild kann
    beim ersten Mal ein paar Sekunden laden."""
    try:
        if os.name != "nt" or not os.path.exists(IL_POPUP_FILE):
            return
        lang = _heartbeat_lang()
        strings = _IL_POPUP_STRINGS.get(lang) or _IL_POPUP_STRINGS["en"]
        agent = name or "Agent"
        icon = None
        if agent_id and UUID_RE.fullmatch(str(agent_id)):
            url = "https://media.valorant-api.com/agents/%s/displayicon.png" % str(agent_id).lower()
            if img_get(url):
                path = os.path.join(IMG_CACHE_DIR, hashlib.sha256(url.encode("utf-8")).hexdigest())
                icon = path if os.path.exists(path) else None
        cfg = {"eyebrow": "INSTALOCK", "title": strings["title"].format(agent=agent),
               "body": strings["body"].format(agent=agent), "skip": strings["skip"].format(n=IL_FREE_SKIP),
               "yes": strings["yes"], "no": strings["no"], "icon": icon, "font": _IL_POPUP_FONTS.get(lang),
               "parentPid": os.getpid(), "timeout": max(30, IL_ASK_MAX - (time.time() - ask_at)) + 30,
               "errFile": IL_POPUP_ERR}
        _save_json(IL_POPUP_CFG, cfg)
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        with _il_popup_lock:
            with _il_lock:
                still = _il["status"] == "ask" and _il["askAt"] == ask_at
            old = _il_popup["proc"]
            if not still or (old and old.poll() is None):
                return
            _il_popup["proc"] = subprocess.Popen([exe, IL_POPUP_FILE, IL_POPUP_CFG], cwd=os.path.dirname(IL_POPUP_FILE),
                                                 creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        report_error("il-popup", e)


def _il_popup_poll():
    """Antwort aus dem Fenster abholen. None = noch offen, geschlossen oder Fehler."""
    with _il_popup_lock:
        proc = _il_popup["proc"]
        if not proc:
            return None
        code = proc.poll()
        if code is None:
            return None
        _il_popup["proc"] = None
    if code in _IL_POPUP_EXIT:
        return _IL_POPUP_EXIT[code]
    tail = ""
    try:
        with open(IL_POPUP_ERR, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-400:]
        os.remove(IL_POPUP_ERR)
    except OSError:
        pass
    report_error("il-popup", RuntimeError("Fenster beendet mit Code %s %s" % (code, tail.strip())))
    return None


def _il_popup_close():
    """Fenster weg (Antwort kam aus dem Overlay, Pause, neue Auswahl, ...)."""
    with _il_popup_lock:
        proc, _il_popup["proc"] = _il_popup["proc"], None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def _il_finish(ok, message):
    with _il_lock:
        _il.update({"on": False, "at": time.time(), "fireAt": 0.0,
                    "status": "done" if ok else "error", "message": message,
                    "gen": _il["gen"] + 1})
        _il_save()


def _instalock_tick():
    with _il_lock:
        if not _il["on"]:
            return
        status, agent, name = _il["status"], _il["agent"], _il["name"]
        mid, fire_at = _il["match"], _il["fireAt"]
        delay, jitter = _il["delay"], _il["jitter"]
        locked_at = _il["lockedAt"]
        free, ingame_mid, ingame_at = _il["free"], _il["ingameMatch"], _il["ingameAt"]
        ask_at, asked = _il["askAt"], _il["asked"]

    # --- Phase 1: auf eine Agentenauswahl warten ---
    if status == "armed":
        now = time.time()
        if now - _il_poll["ts"] < IL_POLL:
            return
        _il_poll["ts"] = now
        if not game_ready():
            return
        try:
            found = _pregame_id_fresh()
        except Exception:
            return              # Token/Region kurz weg -> im naechsten Takt erneut
        if not found:
            return
        wait = max(0.0, delay + random.uniform(-jitter, jitter))
        with _il_lock:
            if not _il["on"] or _il["status"] != "armed":
                return          # zwischenzeitlich abgeschaltet oder neu gesetzt
            _il.update({"match": found, "fireAt": time.time() + wait,
                        "planned": wait, "status": "waiting",
                        "message": "Agentenauswahl erkannt — %s wird in %.1f s gesperrt."
                                   % (name or "Agent", wait),
                        "gen": _il["gen"] + 1})
        return

    # --- Phase 2: Verzögerung abwarten, dann sperren ---
    if status == "waiting":
        if not mid:
            return
        if time.time() < fire_at:
            return

        ok, err = False, "Sperren fehlgeschlagen."
        for _ in range(IL_LOCK_TRIES):
            try:
                ok, err = pick_agent(mid, "lock", agent)
            except Exception as e:
                ok, err = False, "%s: %s" % (type(e).__name__, e)
            if ok:
                break
            with _il_lock:
                if not _il["on"] or _il["match"] != mid:
                    return      # abgeschaltet waehrend der Versuche
            time.sleep(IL_LOCK_GAP)
        if not ok:
            _il_finish(False, err)
            return
        # Gesperrt — aber noch nicht sicher im echten Spiel: Wird das Match
        # gedodged, landet man zurück in einer neuen Agentenauswahl. Der
        # Instalock bleibt darum scharf, bis das echte Spiel bestätigt ist.
        with _il_lock:
            if not _il["on"]:
                return
            _il.update({"status": "locked", "lockedAt": time.time(),
                        "message": "%s gesperrt — wartet auf Spielstart." % (name or "Agent"),
                        "gen": _il["gen"] + 1})
        return

    # --- Phase 3: gesperrt, auf den echten Spielstart warten (oder einen Dodge) ---
    if status == "locked":
        now = time.time()
        if now - locked_at > IL_LOCKED_TIMEOUT:
            _il_finish(True, "%s gesperrt." % (name or "Agent"))
            return
        if now - _il_poll["ts"] < IL_POLL:
            return
        _il_poll["ts"] = now
        try:
            auth = get_auth()
            cg_status, cg = riot_get("glz", "/core-game/v1/players/%s" % auth["puuid"])
        except Exception:
            cg_status, cg = None, None
        if cg_status == 200 and cg and cg.get("MatchID"):
            # Im Spiel: Instalock bleibt an, nach dem Match kommt die Frage (Phase 4/5)
            with _il_lock:
                if not _il["on"] or _il["status"] != "locked":
                    return
                _il.update({"status": "ingame", "ingameMatch": cg["MatchID"], "ingameAt": time.time(),
                            "message": "%s gesperrt — im Spiel." % (name or "Agent"),
                            "gen": _il["gen"] + 1})
                _il_save()
            return
        try:
            found = _pregame_id_fresh()
        except Exception:
            found = None
        if found and found != mid:
            # Match gedodged: neue Agentenauswahl für denselben Wunsch-Agenten.
            wait = max(0.0, delay + random.uniform(-jitter, jitter))
            with _il_lock:
                if not _il["on"]:
                    return
                _il.update({"match": found, "fireAt": time.time() + wait,
                            "planned": wait, "status": "waiting",
                            "message": "Match gedodged — neue Auswahl erkannt, %s wird in %.1f s gesperrt."
                                       % (name or "Agent", wait),
                            "gen": _il["gen"] + 1})
        return

    # --- Phase 4: im Match, auf das Ende warten (Stand vom _game_watcher) ---
    if status == "ingame":
        now = time.time()
        if now - ingame_at > IL_INGAME_MAX:
            _il_pause("Matchende nicht erkannt — Instalock pausiert.")
            return
        st, gmid, since = _game["state"], _game["matchId"], _game["since"]
        # Erst ein Wechsel NACH dem Spielstart zaehlt - der Waechter tickt nur alle 5 s
        # und kann kurz noch PREGAME zeigen.
        if st == "unknown" or since <= ingame_at or (st == "INGAME" and gmid == ingame_mid):
            return
        if st == "offline":
            _il_pause("VALORANT geschlossen — Instalock pausiert.")
            return
        left = free - 1
        with _il_lock:
            if not _il["on"] or _il["status"] != "ingame":
                return
            if left > 0:
                _il.update({"status": "armed", "free": left, "match": None, "fireAt": 0.0,
                            "planned": 0.0, "lockedAt": 0.0, "ingameMatch": None,
                            "message": "Noch %d Match(es) ohne Nachfrage." % left,
                            "gen": _il["gen"] + 1})
            else:
                _il.update({"status": "ask", "free": 0, "askAt": now, "asked": False,
                            "ingameMatch": None, "message": "Weiter mit %s?" % (name or "Agent"),
                            "gen": _il["gen"] + 1})
            _il_save()
        _il_poll["ts"] = 0.0
        return

    # --- Phase 5: nach dem Match fragen ("Weiter mit Jett?") ---
    if status == "ask":
        now = time.time()
        ans = _il_popup_poll()
        if ans:
            instalock_answer(ans)
            return
        # Laeuft das Spiel-Overlay (2.5.5-dev2), fragt es selbst (Karte unten rechts).
        # il_popup.py nur noch, wenn es nicht antwortet - und nie, wenn "Fenster ueber dem
        # Spiel" aus ist. Die Frage im Overlay im Browser gibt es immer.
        if not asked and not _ovl_alive() and ovl_settings()["on"]:
            with _il_lock:
                if _il["status"] != "ask" or _il["asked"]:
                    return
                _il["asked"] = True
            threading.Thread(target=_il_popup_open, args=(agent, name, ask_at), daemon=True).start()
            return
        if now - ask_at > IL_ASK_MAX:
            _il_pause("Keine Antwort — Instalock pausiert.")
            return
        if not game_ready() or (_game["state"] in ("INGAME", "offline") and _game["since"] > ask_at):
            _il_pause("Keine Antwort — Instalock pausiert.")
            return
        if now - _il_poll["ts"] < IL_POLL:
            return
        _il_poll["ts"] = now
        try:
            found = _pregame_id_fresh()
        except Exception:
            return
        if found:
            # Ohne Antwort bis zur naechsten Agentenauswahl: nicht sperren (Wunsch des Nutzers)
            _il_pause("Keine Antwort vor der Agentenauswahl — Instalock pausiert.")
        return


def _instalock_watcher():
    while True:
        time.sleep(IL_TICK)
        try:
            _instalock_tick()
        except Exception as e:
            _il_finish(False, "%s: %s" % (type(e).__name__, e))


# ============================ Spiel-Overlay (2.5.5-dev2) ============================
# Kleine Fenster direkt ueber VALORANT (game_overlay.py, eigener Prozess wie il_popup.py):
# Agentenauswahl mit Raengen und Partys, Spielertabelle per Taste (Standard Tab, wie das
# Scoreboard im Spiel), Ergebnis nach dem Match und die Instalock-Frage. Der Dienst startet
# den Prozess, solange VALORANT laeuft, und liefert unter /api/ovl/state alles fertig
# aufbereitet (Texte in der eingestellten Sprache, Bildpfade aus dem Bildspeicher).
# Riot fragt dafuer fast nichts zusaetzlich: Raenge und Namen kommen aus dem Heartbeat von
# vry.exe (live.json) und den Caches hier. Neu sind nur die Agentenauswahl selbst (glz, so
# oft wie die Seite) und nach einem Competitive-Match EINE competitiveupdates-Abfrage fuer
# die eigene RR-Aenderung.
OVL_FILE = os.path.join(BASE, "game_overlay.py")
OVL_CFG = os.path.join(DATA_DIR, "game-overlay.json")
OVL_ERR = os.path.join(DATA_DIR, "game-overlay-error.txt")
OVL_DEFAULTS = {"on": True, "pregame": True, "board": True, "result": True,
                "key": "tab", "side": "right", "size": "m"}
OVL_BOOLS = ("on", "pregame", "board", "result")
OVL_CHOICES = {"key": ("tab", "capslock", "f1", "f2", "f3", "f4", "x1", "x2"),
               "side": ("right", "left"), "size": ("s", "m", "l")}
OVL_ALIVE = 5.0             # so frisch muss die letzte Abfrage des Overlays sein
OVL_STOP_GRACE = 8.0        # ein tasklist-Aussetzer beendet das Overlay nicht gleich
OVL_CRASHES = 3             # Abstuerze je 10 Minuten, danach kein neuer Start
OVL_RESULT_MAX_AGE = 600    # Analyse aelter als 10 min -> keine Ergebnis-Karte mehr
OVL_PREVIEW = (("pregame", 6.0), ("board", 6.0), ("card", 10.0))
VAPI_TTL = 12 * 3600
_OVL_STRING_KEYS = (
    "ovl_eyebrow_pregame", "ovl_eyebrow_board", "ovl_eyebrow_result", "ovl_team_ally", "ovl_team_enemy",
    "ovl_team_all", "ovl_open_report", "ovl_rank_up", "ovl_rank_down", "ovl_il_in", "ovl_il_locked",
    "ovl_hidden", "ovl_avg", "ovl_preview", "ovl_grade", "report_victory", "report_defeat", "report_draw",
    "report_place", "report_perf_above", "report_perf_below", "report_perf_even", "report_mvp",
    "report_team_mvp", "pregame_you", "pregame_hovering", "instalock_ask_title", "instalock_ask_body",
    "instalock_ask_skip", "instalock_ask_yes", "instalock_ask_no", "peak_label", "side_attack", "side_defense")
# Modus-Namen bleiben Englisch (wie im Overlay)
_OVL_QUEUES = {"competitive": "Competitive", "unrated": "Unrated", "swiftplay": "Swiftplay",
               "spikerush": "Spike Rush", "deathmatch": "Deathmatch", "ggteam": "Escalation",
               "hurm": "Team Deathmatch", "premier": "Premier", "onefa": "Replication",
               "snowball": "Snowball Fight", "newmap": "New Map", "": "Custom"}

_ovl = {"proc": None, "token": secrets.token_urlsafe(24), "seen": 0.0, "crashes": [], "stopAt": 0.0,
        "stopping": False, "previewAt": 0.0, "dismissed": None, "askHidden": None}
_ovl_wake = threading.Event()
_ovl_cfg_cache = {"mtime": None, "data": dict(OVL_DEFAULTS)}
_ovl_str_cache = {}
_ovl_live_cache = {"mtime": None, "data": {}}
_ovl_pg = {"ts": 0.0, "data": None}
_ovl_pg_lock = threading.Lock()
_ovl_rr = {"match": None, "data": None, "tries": 0, "next": 0.0, "busy": False}
_ovl_img_pending = set()
_ovl_img_lock = threading.Lock()
_vapi = {"agents": {}, "byName": {}, "tiers": {}, "maps": {}, "at": 0.0, "busy": False, "fail": 0.0}


def _ovl_settings_from(data):
    raw = data.get("gameOverlay") if isinstance(data, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = dict(OVL_DEFAULTS)
    for k in OVL_BOOLS:
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    for k, allowed in OVL_CHOICES.items():
        if raw.get(k) in allowed:
            out[k] = raw[k]
    return out


def ovl_settings():
    """Einstellungen aus config.json ("gameOverlay"), nur bei geaenderter Datei neu gelesen."""
    try:
        mtime = os.path.getmtime(CONFIG_JSON_FILE)
    except OSError:
        mtime = None
    if mtime != _ovl_cfg_cache["mtime"]:
        _ovl_cfg_cache.update(mtime=mtime, data=_ovl_settings_from(_load_json(CONFIG_JSON_FILE)))
    return dict(_ovl_cfg_cache["data"])


def set_ovl_settings(body, local=True):
    if not local:
        return {"ok": False, "error": "Nur am PC selbst möglich."}
    body = body if isinstance(body, dict) else {}
    with _config_lock:
        data = _load_json(CONFIG_JSON_FILE)
        cur = _ovl_settings_from(data)
        was_on = cur["on"]
        for k in OVL_BOOLS:
            if isinstance(body.get(k), bool):
                cur[k] = body[k]
        for k, allowed in OVL_CHOICES.items():
            if body.get(k) in allowed:
                cur[k] = body[k]
        data["gameOverlay"] = cur
        _save_json(CONFIG_JSON_FILE, data)
    _ovl_cfg_cache["mtime"] = None
    if was_on and not cur["on"]:
        record_feature("game_overlay_off")
    _ovl_wake.set()
    return {"ok": True, "settings": cur}


def ovl_available():
    return os.name == "nt" and os.path.exists(OVL_FILE)


def _ovl_tr(strings, key, **kw):
    s = strings.get(key) or key
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def _ovl_strings(lang):
    hit = _ovl_str_cache.get(lang)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out = {}
    for code in ("en", lang):
        try:
            with open(os.path.join(LANG_DIR, code + ".json"), "r", encoding="utf-8") as fh:
                s = (json.load(fh) or {}).get("strings") or {}
            out.update({k: s[k] for k in _OVL_STRING_KEYS if isinstance(s.get(k), str)})
        except Exception:
            pass
    _ovl_str_cache[lang] = (time.time(), out)
    return out


# ---- Agenten-, Rang- und Kartenbilder (valorant-api.com, wie die Seite) ----
def _vapi_ensure():
    now = time.time()
    if _vapi["busy"] or (_vapi["at"] and now - _vapi["at"] < VAPI_TTL) or now - _vapi["fail"] < 60:
        return
    _vapi["busy"] = True

    def run():
        try:
            _, a = _http("https://valorant-api.com/v1/agents?isPlayableCharacter=true")
            _, t = _http("https://valorant-api.com/v1/competitivetiers")
            _, m = _http("https://valorant-api.com/v1/maps")
            agents = {}
            for x in (a or {}).get("data") or []:
                if isinstance(x, dict) and x.get("uuid"):
                    agents[x["uuid"].lower()] = {"name": x.get("displayName") or "", "icon": x.get("displayIcon")}
            if not agents:
                raise ValueError("keine Agentenliste")
            sets = (t or {}).get("data") or []
            tiers = {}
            for x in (sets[-1].get("tiers") or []) if sets else []:
                if isinstance(x.get("tier"), int):
                    tiers[x["tier"]] = x.get("smallIcon") or x.get("largeIcon")
            maps = {}
            for x in (m or {}).get("data") or []:
                if isinstance(x, dict) and x.get("mapUrl"):
                    maps[x["mapUrl"].lower()] = {"name": x.get("displayName") or "",
                                                 "img": x.get("listViewIcon") or x.get("splash")}
            _vapi.update(agents=agents, tiers=tiers, maps=maps, at=time.time(),
                         byName={v["name"].lower(): k for k, v in agents.items() if v["name"]})
        except Exception:
            _vapi["fail"] = time.time()
        finally:
            _vapi["busy"] = False

    threading.Thread(target=run, daemon=True).start()


def _ovl_img(url):
    """Pfad im Bildspeicher - fehlt das Bild, wird es im Hintergrund geholt (naechste Abfrage)."""
    if not url or not _img_allowed(url):
        return None
    path = os.path.join(IMG_CACHE_DIR, hashlib.sha256(url.encode("utf-8")).hexdigest())
    if os.path.exists(path):
        return path
    with _ovl_img_lock:
        if url in _ovl_img_pending:
            return None
        _ovl_img_pending.add(url)

    def run():
        try:
            img_get(url)
        finally:
            with _ovl_img_lock:
                _ovl_img_pending.discard(url)

    threading.Thread(target=run, daemon=True).start()
    return None


def _ovl_agent_id(agent_id=None, name=None):
    aid = str(agent_id or "").lower()
    if aid and UUID_RE.fullmatch(aid):
        return aid
    return _vapi["byName"].get(str(name or "").lower())


def _ovl_agent_img(agent_id=None, name=None):
    aid = _ovl_agent_id(agent_id, name)
    if not aid:
        return None
    url = (_vapi["agents"].get(aid) or {}).get("icon") or "https://media.valorant-api.com/agents/%s/displayicon.png" % aid
    return _ovl_img(url)


def _ovl_rank_img(tier):
    try:
        url = _vapi["tiers"].get(int(tier or 0))
    except (TypeError, ValueError):
        url = None
    return _ovl_img(url) if url else None


def _ovl_map(path):
    return _vapi["maps"].get(str(path or "").lower()) or {}


# ---- Spielerzeilen ----
def _ovl_live():
    """Letzter Heartbeat von vry.exe (live.json) - nur neu gelesen, wenn die Datei sich aendert."""
    try:
        mtime = os.path.getmtime(LIVE_FILE)
    except OSError:
        return {}
    if mtime != _ovl_live_cache["mtime"]:
        _ovl_live_cache.update(mtime=mtime, data=_load_json(LIVE_FILE) or {})
    return _ovl_live_cache["data"]


def _ovl_heartbeat(state):
    d = _ovl_live()
    hb = d.get("heartbeat") if isinstance(d.get("heartbeat"), dict) else None
    if not hb or hb.get("state") != state:
        return {}
    players = hb.get("players") if isinstance(hb.get("players"), dict) else {}
    return {"me": hb.get("puuid"), "mode": hb.get("mode"), "players": players}


def _ovl_parties(mid):
    """Partys aus der Party-Erkennung des Dienstes (nur gecacht), sonst None -> vry.exe."""
    with _cache_lock:
        hit = _premade_cache
        key, data = hit.get("match") or "", hit.get("data")
    if mid and key.startswith(mid + "|") and isinstance(data, dict) and isinstance(data.get("parties"), dict):
        return data["parties"]
    return None


def _ovl_row(puuid, me, base, hbp, parties):
    hbp = hbp if isinstance(hbp, dict) else {}
    name = hbp.get("name") or base.get("name") or ""
    if not name and puuid:
        name = _names_cached([puuid], time.time())[0].get(puuid) or ""
    tier, rr, peak = hbp.get("rank"), hbp.get("rr"), hbp.get("peakRank")
    if not isinstance(tier, int) and puuid:
        with _cache_lock:
            hit = _mmr_cache.get(puuid)
        mm = hit[1] if hit and isinstance(hit[1], dict) else {}
        tier, rr, peak = mm.get("rank"), mm.get("rr"), mm.get("peakRank")
    tier = tier if isinstance(tier, int) else 0
    if parties is not None:
        party = parties.get(puuid) or 0
    else:
        party = hbp.get("partyNumber") or 0
    aid = _ovl_agent_id(base.get("agent"), hbp.get("agent"))
    agent_name = (_vapi["agents"].get(aid) or {}).get("name") if aid else None
    hidden = _name_hidden(name)
    return {"name": None if hidden else name, "hidden": hidden,
            "agent": agent_name or hbp.get("agent") or "", "agentImg": _ovl_agent_img(aid) if aid else None,
            "locked": bool(base.get("locked")), "hover": bool(base.get("hover")), "me": bool(me),
            "tier": tier, "rr": rr if isinstance(rr, int) else None, "peak": peak if isinstance(peak, int) else 0,
            "level": base.get("level") or hbp.get("level") or None, "party": party if isinstance(party, int) else 0,
            "rankImg": _ovl_rank_img(tier) if tier > 2 else None,
            "kd": hbp.get("kd") if isinstance(hbp.get("kd"), (int, float)) else None,
            "hs": hbp.get("headshotPercentage") if isinstance(hbp.get("headshotPercentage"), (int, float)) else None}


def _ovl_sort(rows):
    """Nach Rang, Partys zusammen (wie groupByParty im Overlay)."""
    rows = sorted(rows, key=lambda r: (-(r["tier"] or 0), -(r["level"] or 0)))
    out, done = [], set()
    for i, r in enumerate(rows):
        if i in done:
            continue
        group = [i] + ([j for j, q in enumerate(rows) if j not in done and j != i and r["party"]
                        and q["party"] == r["party"]])
        for j in group:
            done.add(j)
            out.append(rows[j])
    return out


def _ovl_queue_name(queue, hb_mode=None):
    q = str(queue or "").lower()
    return hb_mode or _OVL_QUEUES.get(q) or q.title()


def _ovl_pregame_raw():
    """Agentenauswahl: fragt die Seite gerade (/api/pregame), deren Stand samt Gegnern -
    sonst selbst eine schlanke Abfrage (ohne Gegner-Raenge/-Historie, die laufen nur mit Seite)."""
    c = _pregame_cache
    if c["data"] and c["data"].get("ok") and time.time() - c["ts"] < 2.5:
        return c["data"]
    with _ovl_pg_lock:
        now = time.time()
        if _ovl_pg["data"] and now - _ovl_pg["ts"] < 0.9:
            return _ovl_pg["data"]
        mid = pregame_match_id()
        if not mid:
            _ovl_pg.update(ts=now, data=None)
            return None
        status, m = riot_get("glz", "/pregame/v1/matches/%s" % mid)
        if status != 200 or not m:
            return None
        raw = (m.get("AllyTeam") or {}).get("Players") or []
        ids = [p.get("Subject") for p in raw if p.get("Subject")]
        names = names_for_nonblocking(ids)
        players = []
        for p in raw:
            if not p.get("Subject"):
                continue
            sel = p.get("CharacterSelectionState") or ""
            players.append({"puuid": p["Subject"], "name": names.get(p["Subject"]),
                            "level": (p.get("PlayerIdentity") or {}).get("AccountLevel"),
                            "agent": p.get("CharacterID") or None, "locked": sel == "locked",
                            "hovering": sel == "selected"})
        team = str((m.get("AllyTeam") or {}).get("TeamID") or "")
        data = {"ok": True, "matchId": mid, "players": players, "enemies": [],
                "side": {"red": "attack", "blue": "defense"}.get(team.lower()),
                "map": (m.get("MapID") or "").lower() or None, "queue": m.get("QueueID") or ""}
        _ovl_pg.update(ts=time.time(), data=data)
        return data


def _ovl_il_chip(strings, mid):
    with _il_lock:
        on, st, name, fire_at, match = _il["on"], _il["status"], _il["name"], _il["fireAt"], _il["match"]
    if not on or not mid or match != mid:
        return None
    if st == "waiting":
        n = max(0, int(math.ceil(fire_at - time.time())))
        return {"text": _ovl_tr(strings, "ovl_il_in", name=name or "Agent", n=n)}
    if st in ("locked", "ingame"):
        return {"text": _ovl_tr(strings, "ovl_il_locked", name=name or "Agent")}
    return None


def _ovl_pregame(strings):
    data = _ovl_pregame_raw()
    if not data or not data.get("ok"):
        return None
    mid = data.get("matchId")
    hb = _ovl_heartbeat("PREGAME")
    hbp = hb.get("players") or {}
    me = _account["puuid"] or hb.get("me")
    parties = _ovl_parties(mid)
    allies = [_ovl_row(p.get("puuid"), p.get("puuid") == me,
                       {"agent": p.get("agent"), "name": p.get("name"), "locked": p.get("locked"),
                        "hover": p.get("hovering"), "level": p.get("level")}, hbp.get(p.get("puuid")), parties)
              for p in data.get("players") or [] if p.get("puuid")]
    enemies = []
    for p in data.get("enemies") or []:
        if not isinstance(p, dict) or not p.get("puuid"):
            continue
        enemies.append(_ovl_row(p["puuid"], False, {"agent": p.get("agent"), "name": p.get("name"), "locked": True},
                                {"rank": p.get("rank"), "rr": p.get("rr"), "peakRank": p.get("peakRank")}, parties))
    side = data.get("side")
    return {"id": mid, "map": _ovl_map(data.get("map")).get("name") or "",
            "mode": _ovl_queue_name(data.get("queue"), hb.get("mode")) if (data.get("queue") or hb.get("mode")) else "",
            "sideText": _ovl_tr(strings, "side_attack" if side == "attack" else "side_defense") if side else "",
            "il": _ovl_il_chip(strings, mid), "allies": _ovl_sort(allies), "enemies": _ovl_sort(enemies)}


def _ovl_board():
    mid = _game["matchId"]
    hb = _ovl_heartbeat("INGAME")
    hbp = hb.get("players") or {}
    roster = _game_roster if (mid and _game_roster["match"] == mid and _game_roster["players"]) else None
    if not roster and not hbp:
        return None
    me = _account["puuid"] or hb.get("me")
    parties = _ovl_parties(mid)
    allies, enemies = [], []
    if roster:
        for p in roster["players"]:
            row = _ovl_row(p["puuid"], p["puuid"] == me, {"agent": p.get("agent")}, hbp.get(p["puuid"]), parties)
            (allies if p.get("ally") else enemies).append(row)
        queue, map_path = roster.get("queue"), roster.get("map")
    else:
        my_team = (hbp.get(me) or {}).get("team")
        for pu, p in hbp.items():
            row = _ovl_row(pu, pu == me, {}, p, parties)
            (allies if (p.get("team") == my_team and my_team) or pu == me else enemies).append(row)
        queue, map_path = "", None
    ffa = str(queue or "").lower() == "deathmatch" or (len(allies) <= 1 and len(enemies) > 5)
    if ffa:
        allies, enemies = allies + enemies, []
    return {"id": mid, "map": _ovl_map(map_path).get("name") or "", "ffa": ffa,
            "mode": _ovl_queue_name(queue, hb.get("mode")) if (queue or hb.get("mode")) else "",
            "allies": _ovl_sort(allies), "enemies": _ovl_sort(enemies)}


def _ovl_grade(r):
    if not isinstance(r, (int, float)):
        return None
    return "S" if r >= 1.30 else "A" if r >= 1.12 else "B" if r >= 0.95 else "C" if r >= 0.80 else "D"


def _ovl_rr_get(mid):
    """Eigene RR-Aenderung des Matches (einmal competitiveupdates, ein paar Versuche -
    Riot traegt das Match dort erst kurz nach dem Ende ein)."""
    st = _ovl_rr
    if st["match"] != mid:
        st.update(match=mid, data=None, tries=0, next=0.0, busy=False)
    if st["data"] is not None or st["busy"] or st["tries"] >= 4 or time.time() < st["next"]:
        return st["data"]
    st["busy"] = True

    def run():
        try:
            me = get_auth()["puuid"]
            status, j = riot_get("pd", "/mmr/v1/players/%s/competitiveupdates?startIndex=0&endIndex=3"
                                       "&queue=competitive" % me)
            hit = next((x for x in (j or {}).get("Matches") or [] if x.get("MatchID") == mid), None) \
                if status == 200 and isinstance(j, dict) else None
            if hit and _ovl_rr["match"] == mid and isinstance(hit.get("TierAfterUpdate"), int):
                _ovl_rr["data"] = {"earned": hit.get("RankedRatingEarned"), "tier": hit.get("TierAfterUpdate"),
                                   "rr": hit.get("RankedRatingAfterUpdate"), "before": hit.get("TierBeforeUpdate")}
        except Exception:
            pass
        finally:
            _ovl_rr.update(tries=_ovl_rr["tries"] + 1, next=time.time() + 20.0, busy=False)

    threading.Thread(target=run, daemon=True).start()
    return None


def _ovl_result(strings):
    if _game["state"] in ("PREGAME", "INGAME"):
        return None
    data = _report_current()
    if not data or not data.get("matchId") or data["matchId"] == _ovl["dismissed"]:
        return None
    if time.time() - float(data.get("createdAt") or 0) > OVL_RESULT_MAX_AGE:
        return None
    me = next((p for p in data.get("players") or [] if p.get("me")), None)
    if not me:
        return None
    mid, queue = data["matchId"], str(data.get("queue") or "")
    rating = me.get("rating")
    perf = ""
    if isinstance(rating, (int, float)):
        pct = int(round((rating - 1) * 100))
        perf = (_ovl_tr(strings, "report_perf_above", n=pct) if pct >= 2 else
                _ovl_tr(strings, "report_perf_below", n=-pct) if pct <= -2 else _ovl_tr(strings, "report_perf_even"))
    mapinfo = _ovl_map(data.get("map"))
    mapline = mapinfo.get("name") or ""
    if isinstance(data.get("durationMs"), (int, float)) and data["durationMs"] > 0:
        mapline = (mapline + "  ·  " if mapline else "") + "%d min" % round(data["durationMs"] / 60000.0)
    stats = [["K / D / A", "%s / %s / %s" % (me.get("kills", 0), me.get("deaths", 0), me.get("assists", 0))]]
    if isinstance(me.get("acs"), (int, float)):
        stats.append(["ACS", "%d" % round(me["acs"])])
    if isinstance(me.get("hs"), (int, float)):
        stats.append(["HS", "%d %%" % round(me["hs"])])
    if isinstance(me.get("adr"), (int, float)):
        stats.append(["ADR", "%d" % round(me["adr"])])
    elif isinstance(me.get("kd"), (int, float)):
        stats.append(["K/D", "%.2f" % me["kd"]])
    rr = _ovl_rr_get(mid) if queue == "competitive" else None
    return {"id": mid, "result": data.get("result"), "score": data.get("score") or {},
            "map": mapline, "mapImg": _ovl_img(mapinfo.get("img")) if mapinfo.get("img") else None,
            "mode": _ovl_queue_name(queue), "agentImg": _ovl_agent_img(me.get("agent")),
            "grade": _ovl_grade(rating), "perf": perf, "mvp": me.get("mvp"), "stats": stats,
            "rr": dict(rr, img=_ovl_rank_img(rr.get("tier"))) if rr else None}


def _ovl_ask():
    with _il_lock:
        if not (_il["on"] and _il["status"] == "ask"):
            return None
        ask_at, agent, name = _il["askAt"], _il["agent"], _il["name"]
    with _il_popup_lock:
        popup = _il_popup["proc"]
    if ask_at == _ovl["askHidden"] or (popup and popup.poll() is None):
        return None             # weggeklickt bzw. das alte Fenster (il_popup.py) fragt schon
    return {"id": ask_at, "name": name or "Agent", "img": _ovl_agent_img(agent), "skipN": IL_FREE_SKIP}


# ---- Vorschau (Einstellungen -> "Vorschau zeigen") ----
_OVL_SAMPLE = (  # Agent, Name, Rang, RR, Peak, Level, Party
    ("add6443a-41bd-e414-f6ad-e58d267f4e95", None, 16, 47, 19, 213, 1),
    ("320b2a48-4d9b-a075-30f1-1f93a9b638fa", "Nova#1337", 17, 12, 17, 88, 1),
    ("8e253930-4c05-31dd-1b6c-968525494517", "Atlas#777", 14, 81, 21, 340, 0),
    ("1e58de9c-4950-5125-93e9-a0aee9f98746", "Mango#GG", 12, 3, 14, 41, 0),
    ("569fdd95-4d10-43ab-ca70-79becc718b46", "Echo#404", 15, 66, 15, 152, 0),
    ("f94c3b30-42be-e959-889c-5aa313dba261", "Raze Main#TR1", 19, 66, 21, 156, 2),
    ("a3bfb853-43b2-7238-a4f1-ad90e9e46bcc", "Reyna Diff#KR", 21, 15, 22, 402, 0),
    ("9f0d8ba9-4140-b941-57d3-a7ad57c6b417", "Brim#smoke", 16, 20, 16, 220, 2),
    ("eb93336a-449b-9c1b-0a54-a891f7921d69", "phoenix#1", 14, 50, 16, 63, 0),
    ("117ed9e3-49f3-6512-3ccf-0cada7e3823b", "Cypher#cam", 17, 90, 18, 97, 0),
)


def _ovl_preview_step(now):
    t = now - _ovl["previewAt"]
    if t < 0:
        return None
    for name, dur in OVL_PREVIEW:
        if t < dur:
            return name
        t -= dur
    return None


def _ovl_preview_data(step, strings):
    me_name = _self_name.get("name") or "RankYoinker#DEMO"
    rows = []
    for i, (aid, name, tier, rr, peak, level, party) in enumerate(_OVL_SAMPLE):
        rows.append({"name": name or me_name, "hidden": False, "agent": (_vapi["agents"].get(aid) or {}).get("name") or "",
                     "agentImg": _ovl_agent_img(aid), "locked": i != 2, "hover": i == 2, "me": i == 0, "tier": tier,
                     "rr": rr, "peak": peak, "level": level, "party": party, "rankImg": _ovl_rank_img(tier),
                     "kd": round(0.8 + (i * 37 % 9) / 10.0, 2), "hs": 14 + (i * 7) % 18})
    pid = "preview-%d" % int(_ovl["previewAt"])
    ascent = _ovl_map("/game/maps/ascent/ascent")
    if step == "pregame":
        allies = [dict(r, kd=None, hs=None) for r in rows[:5]]
        return {"pregame": {"id": pid, "map": ascent.get("name") or "Ascent", "mode": "Competitive",
                            "sideText": _ovl_tr(strings, "side_attack"),
                            "il": {"text": _ovl_tr(strings, "ovl_il_in", name=rows[0]["agent"] or "Jett", n=7)},
                            "allies": allies, "enemies": []}}
    if step == "board":
        board = [dict(r, hover=False, locked=True) for r in rows]
        return {"board": {"id": pid, "map": ascent.get("name") or "Ascent", "mode": "Competitive", "ffa": False,
                          "allies": _ovl_sort(board[:5]), "enemies": _ovl_sort(board[5:])}}
    agent = rows[0]["agent"] or "Jett"
    return {"result": {"id": pid, "result": "win", "score": {"ally": 13, "enemy": 9},
                       "map": (ascent.get("name") or "Ascent") + "  ·  34 min",
                       "mapImg": _ovl_img(ascent.get("img")) if ascent.get("img") else None, "mode": "Competitive",
                       "agentImg": rows[0]["agentImg"], "grade": "A", "perf": _ovl_tr(strings, "report_perf_above", n=18),
                       "mvp": "team", "stats": [["K / D / A", "21 / 14 / 6"], ["ACS", "268"], ["HS", "24 %"], ["ADR", "151"]],
                       "rr": {"earned": 18, "tier": 16, "rr": 47, "before": 15, "img": _ovl_rank_img(16)}},
            "ask": {"id": pid, "name": agent, "img": rows[0]["agentImg"], "skipN": IL_FREE_SKIP}}


def ovl_preview(local=True):
    if not local:
        return {"ok": False, "error": "Nur am PC selbst möglich."}
    if not ovl_available():
        return {"ok": False, "error": "unavailable"}
    _vapi_ensure()
    _ovl["previewAt"] = time.time()
    _ovl["crashes"] = []                    # bewusst ausprobiert -> wieder starten duerfen
    _ovl_wake.set()
    record_feature("game_overlay_preview")
    return {"ok": True, "seconds": int(sum(d for _, d in OVL_PREVIEW))}


# ---- Was das Overlay abfragt / meldet ----
def ovl_state():
    now = time.time()
    _ovl["seen"] = now
    s = ovl_settings()
    lang = _heartbeat_lang()
    strings = _ovl_strings(lang)
    _vapi_ensure()
    out = {"ok": True, "v": 1, "settings": s, "lang": lang, "strings": strings, "phase": _game["state"],
           "preview": None, "pregame": None, "board": None, "result": None, "ask": None}
    step = _ovl_preview_step(now)
    if step:
        out["preview"] = step
        out.update(_ovl_preview_data(step, strings))
        return out
    if not s["on"]:
        return out
    for key, want, fn in (("pregame", s["pregame"] and _game["state"] == "PREGAME", lambda: _ovl_pregame(strings)),
                          ("board", s["board"] and _game["state"] == "INGAME", _ovl_board),
                          ("result", s["result"], lambda: _ovl_result(strings)),
                          ("ask", True, _ovl_ask)):
        if not want:
            continue
        try:
            out[key] = fn()
        except RiotError:
            pass
        except Exception as e:
            report_error("game-overlay", e)
    return out


def ovl_action(body):
    body = body if isinstance(body, dict) else {}
    act = body.get("action")
    if act == "il":
        with _il_lock:
            cur = _il["askAt"]
        if body.get("id") is not None and body.get("id") != cur:
            return {"ok": False, "error": "stale"}     # Vorschau oder schon eine neue Frage
        return instalock_answer(str(body.get("answer") or ""))
    if act == "dismiss":
        if body.get("kind") == "result":
            _ovl["dismissed"] = str(body.get("id") or "")
        elif body.get("kind") == "ask":
            _ovl["askHidden"] = body.get("id")
        return {"ok": True}
    if act == "open":
        _ovl["dismissed"] = str(body.get("id") or "")
        threading.Thread(target=open_overlay_in_browser, daemon=True).start()
        record_feature("game_overlay_open_report")
        return {"ok": True}
    return {"ok": False, "error": "Unbekannte Aktion."}


def _ovl_alive():
    proc = _ovl["proc"]
    return bool(proc and proc.poll() is None and time.time() - _ovl["seen"] < OVL_ALIVE)


# ---- Prozess ----
def _ovl_err_tail():
    tail = ""
    try:
        with open(OVL_ERR, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-600:]
        os.remove(OVL_ERR)
    except OSError:
        pass
    return tail.strip()


def _ovl_should_run(now):
    if now - _ovl["previewAt"] < sum(d for _, d in OVL_PREVIEW) + 2:
        return True
    # Zustand vom _game_watcher (alle 5 s, prueft game_ready) - hier kein eigenes tasklist
    return ovl_settings()["on"] and _game["state"] in ("MENUS", "PREGAME", "INGAME")


def _ovl_start(now):
    try:
        _save_json(OVL_CFG, {"port": PORT, "token": _ovl["token"], "parentPid": os.getpid(), "errFile": OVL_ERR})
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        _ovl.update(stopping=False, seen=0.0, stopAt=0.0)
        _ovl["proc"] = subprocess.Popen([exe, OVL_FILE, OVL_CFG], cwd=BASE, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        _ovl["crashes"].append(now)
        report_error("game-overlay", e)


def _ovl_stop():
    proc = _ovl["proc"]
    _ovl["stopping"] = True
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def _ovl_tick(now):
    if not ovl_available():
        return
    proc = _ovl["proc"]
    if proc and proc.poll() is not None:
        code = proc.returncode
        _ovl["proc"] = proc = None
        tail = _ovl_err_tail()
        if not _ovl["stopping"] and (code != 0 or tail):
            if code != 0:
                _ovl["crashes"].append(now)
            report_error("game-overlay", RuntimeError("Overlay beendet mit Code %s %s" % (code, tail)))
    if _ovl_should_run(now):
        _ovl["stopAt"] = 0.0
        if not proc:
            _ovl["crashes"] = [t for t in _ovl["crashes"] if now - t < 600]
            if len(_ovl["crashes"]) < OVL_CRASHES:
                _ovl_start(now)
    elif proc:
        if not _ovl["stopAt"]:
            _ovl["stopAt"] = now + OVL_STOP_GRACE
        elif now >= _ovl["stopAt"]:
            _ovl_stop()


def _ovl_watcher():
    while True:
        _ovl_wake.wait(2.0)
        _ovl_wake.clear()
        try:
            _ovl_tick(time.time())
        except Exception as e:
            report_error("game-overlay", e)


# ============================ Account-Wechsel erkennen ============================
# Wer den Account wechselt, bekommt sonst minutenlang die Daten des alten
# Kontos zu sehen (Token, Namen, Party, Presets, Shop hängen alle an der puuid).
# Ein kleiner Wächter prüft daher im Sekundentakt, wem der Riot Client gerade
# gehört, und wirft bei einem Wechsel ALLE Caches weg.

_account = {"puuid": None, "gen": 0, "ts": 0.0, "name": None}
ACCOUNT_INTERVAL = 4.0


def _peek_subject():
    """Nur Lockfile + Entitlements — bewusst ohne das grosse ShooterGame.log."""
    try:
        if not os.path.exists(LOCKFILE):
            return None
        with open(LOCKFILE, "r", encoding="utf-8", errors="replace") as fh:
            parts = fh.read().strip().split(":")
        if len(parts) < 5:
            return None
        basic = base64.b64encode(("riot:" + parts[3]).encode()).decode()
        status, ent = _http("https://127.0.0.1:%s/entitlements/v1/token" % parts[2],
                            {"Authorization": "Basic " + basic}, timeout=5, local_unverified=True)
        if status == 200 and ent:
            return ent.get("subject")
    except Exception:
        return None
    return None


def _clear_account_caches():
    """Alles wegwerfen, was an einer puuid haengt."""
    with _auth_lock:
        _auth["data"] = None
        _auth["ts"] = 0.0
    # Besitz/Shop/Loadout liegen seit 2.5.4 je Account (acct_cached)
    with _cache_lock:
        _stats_cache.clear()
        _match_cache.clear()
        _rr_cache.clear()
    _pregame_cache["data"] = None
    _pregame_invalidate()
    _pregame_mid["id"] = None
    _pregame_mid["ts"] = 0.0
    _self_name["puuid"] = None
    _self_name["name"] = None


def note_account(puuid):
    """Von get_auth() aufgerufen: gesehene puuid festhalten (Wechsel-Generation
    fuer die Oberflaeche, siehe game_ready()/gen weiter unten)."""
    if not puuid:
        return
    if _account["puuid"] != puuid:
        if _account["puuid"] is not None:
            _account["gen"] += 1
        _account["puuid"] = puuid
        _account["name"] = None
    _account["ts"] = time.time()


def account_state():
    return {"puuid": _account["puuid"], "gen": _account["gen"]}


def _account_watcher():
    while True:
        time.sleep(ACCOUNT_INTERVAL)
        try:
            subj = _peek_subject()
            if not subj or subj == _account["puuid"]:
                continue
            first = _account["puuid"] is None
            _clear_account_caches()
            _account["puuid"] = subj
            _account["name"] = None
            _account["ts"] = time.time()
            if first:
                continue
            _account["gen"] += 1            # Oberflaeche laedt daraufhin neu

            # vry.exe selbst hält Namen, Ranks und Party des ALTEN Kontos fest
            # — es liest den Account nur beim Start. Darum bekommt es einen
            # sauberen Neustart, sonst stehen in der Lobby weiter alte Namen.
            with _proc_lock:
                if _proc["desired"] and vry_running():
                    _kill_all_vry()
                    time.sleep(1.0)
                    start_vry(force=True)
                    _proc["note"] = ("Account gewechselt — RankYoinker wurde neu gestartet, "
                                     "damit die Namen stimmen.")
        except Exception:
            pass


# ---- Skins/Shop vorladen (2.5.4) ----
# Nutzerwunsch: Die Reiter sollen schon fertig sein, wenn man sie anklickt - sonst
# gab es waehrend einer Riot-Sperre nur Fehler. Laeuft, sobald ein Account erkannt
# ist (Start oder Wechsel), holt nur, was fehlt oder veraltet ist (Shop: erst nach
# dem Tageswechsel), nie in der Agentenauswahl oder den ersten 2 Minuten eines
# Matches (die gehoeren den Spielerdaten) und in der untersten Stufe der Riot-Bremse.
WARM_RETRY = 60.0
WARM_MATCH_SETTLE = 120.0


@_bulk_priority
def _warm_account(puuid):
    """True, wenn alles frisch da ist (ein "stale" = letzter Stand nach Fehlschlag zaehlt nicht)."""
    good = lambda r: bool(r.get("ok")) and not r.get("stale")
    ok = True
    ts, ent = acct_cached(puuid, "ent")
    if not ent or time.time() - ts > WARM_ENT_MAX_AGE:
        ok = good(entitlements(force=True)) and ok
    ok = good(get_shop()) and ok                    # fragt nur, wenn der gemerkte Shop abgelaufen ist
    if not acct_cached(puuid, "loadout")[1]:
        ok = good(get_loadout()) and ok
    if not acct_cached(puuid, "agents")[1]:
        owned_agents()
    return ok


def _account_warmup():
    done, next_try = None, 0.0
    while True:
        time.sleep(5)
        try:
            puuid, now = _account["puuid"], time.time()
            if not puuid or now < next_try or not game_ready():
                continue
            st = _game["state"]
            if st == "PREGAME" or (st == "INGAME" and now - _game["since"] < WARM_MATCH_SETTLE):
                continue
            if pd_gate_state()["paused"]:
                continue
            if done == puuid:
                # schon vorgeladen: nur den Shop nach dem Tageswechsel nachholen
                ts, shop = acct_cached(puuid, "shop")
                if not shop or not _shop_expired(shop, now - ts) or st != "MENUS":
                    continue
            if get_auth()["puuid"] != puuid:
                continue
            if _warm_account(puuid):
                done = puuid
            else:
                next_try = time.time() + WARM_RETRY
        except Exception:
            next_try = time.time() + WARM_RETRY


# ============================ Log-Auswertung ============================

RE_STATE = re.compile(r"(?:first|new) game state: (\w+)")
RE_SCOREBOARD = re.compile(r"getting new (\w+) scoreboard")
RE_MMR_PLAYER = re.compile(r"/mmr/v1/players/([0-9a-f-]{36})(?!/)")
RE_CONN_ERR = re.compile(r"Connection error, retrying")
RE_PARTY = re.compile(r"retrieved party members: (\[.*\])")


def parse_progress(lines):
    """Liest aus dem Log-Ende, was vry gerade macht -> Ladefortschritt fuers Overlay."""
    tail = lines[-400:]
    info = {"phase": "idle", "state": "", "loaded": 0, "total": 0}

    recent = [l for l in tail[-12:] if l.strip()]
    if recent and any(RE_CONN_ERR.search(l) for l in recent[-6:]):
        info["phase"] = "waiting_riot"
        return info

    start_idx, state = -1, ""
    for i, line in enumerate(tail):
        m = RE_SCOREBOARD.search(line)
        if m:
            start_idx, state = i, m.group(1)
    if start_idx == -1:
        m = None
        for line in tail:
            s = RE_STATE.search(line)
            if s:
                m = s
        if m:
            info["state"] = m.group(1)
            info["phase"] = "starting"
        return info

    info["state"] = state
    after = tail[start_idx:]
    puuids = set()
    for line in after:
        m = RE_MMR_PLAYER.search(line)
        if m:
            puuids.add(m.group(1))
    total = 10 if state == "INGAME" else 5 if state == "PREGAME" else 1
    if state == "MENUS":
        for line in reversed(after):
            m = RE_PARTY.search(line)
            if m:
                total = max(1, m.group(1).count("'Subject'"))
                break
    info["loaded"] = min(len(puuids), total)
    info["total"] = total
    info["puuids"] = sorted(puuids)          # wer schon geladen ist (Lade-Ansicht hakt ab)

    if any("Traceback" in l for l in after):
        info["phase"] = "error"
    elif len(puuids) >= total:
        info["phase"] = "done"
    else:
        info["phase"] = "loading"
    return info


# ============================ Match-Wächter ============================
# vry.exe liest den Spielzustand nur, wenn es den Wechsel selbst mitbekommt.
# Startet es mitten in einem laufenden Match — oder war der Riot Client beim
# Start noch nicht bereit — bleibt die Oberfläche leer, bis man von Hand neu
# startet. Genau das nimmt dieser Wächter ab: Er fragt Riot im Hintergrund
# nach dem echten Zustand und startet vry.exe nur dann neu, wenn es
# nachweislich etwas verschlafen hat (mit Karenz- und Abklingzeit, damit
# daraus keine Neustart-Schleife wird).

_game = {"state": "unknown", "matchId": None, "ts": 0.0, "since": 0.0}
GAME_INTERVAL = 5.0
STALE_AFTER = 12.0        # so lange darf vry.exe hinterherhinken
SYNC_COOLDOWN = 90.0      # Mindestabstand zweier Nachhol-Neustarts
START_SETTLE = 25.0       # frisch gestartetes vry.exe darf erst mal laden
_sync = {"last": 0.0, "match": None, "mismatch": 0.0}


def _live_state():
    """Echter Spielzustand laut Riot: INGAME / PREGAME / MENUS."""
    auth = get_auth()
    status, j = riot_get("glz", "/core-game/v1/players/%s" % auth["puuid"])
    if status == 200 and j and j.get("MatchID"):
        return "INGAME", j["MatchID"]
    status, j = riot_get("glz", "/pregame/v1/players/%s" % auth["puuid"])
    if status == 200 and j and j.get("MatchID"):
        # Der Agentenauswahl-Cache bekommt die frische ID gleich mit
        _pregame_note(j["MatchID"])
        return "PREGAME", j["MatchID"]
    return "MENUS", None


_game_roster = {"match": None, "map": "", "queue": "", "players": []}


def game_state():
    """Spielzustand fuer die Oberflaeche — unabhaengig davon, was vry.exe sieht."""
    out = {"state": _game["state"], "matchId": _game["matchId"],
           "ready": game_ready(), "at": int(_game["ts"]), "riotPause": pd_gate_state(),
           "snapshot": {"match": _snapshot["match"], "restored": _snapshot["restored"]},
           "pdRequests": dict(_pd_counts), "report": match_report_state(),
           # Diagnose: wann die Seite zuletzt /status fragte, wann vorgeladen wurde
           "page": {"seenAgo": int(time.time() - _page_seen["ts"]) if _page_seen["ts"] else None,
                    "prefetch": _prefetch_done["key"],
                    "prefetchAgo": int(time.time() - _prefetch_done["at"]) if _prefetch_done["at"] else None}}
    if _game["state"] == "INGAME" and _game_roster["match"] and _game_roster["match"] == _game["matchId"]:
        out["roster"] = {"map": _game_roster["map"], "queue": _game_roster["queue"],
                         "players": _game_roster["players"]}
    return out


def _vry_view():
    """Was glaubt vry.exe gerade? — aus seinem eigenen Log gelesen.

    'log' und 'lines' kommen mit, damit der Haenger-Waechter am Zeilenstand
    erkennt, ob sich ueberhaupt noch etwas tut.
    """
    path = newest_log()
    if not path:
        return {"phase": "", "state": "", "log": None, "lines": 0}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception:
        return {"phase": "", "state": "", "log": None, "lines": 0}
    info = parse_progress(lines)
    info["log"] = path
    info["lines"] = len(lines)
    return info


# ---- Hänger beim Start ----
# Trotz aller Vorsicht kann vry.exe stumm stehenbleiben (beobachtet nach einem
# Kontowechsel: letzte Zeile 'opened lockfile', danach nie wieder etwas). Der
# Prozess lebt, also greift weder der Watchdog noch der Nachhol-Neustart. Von
# aussen ist das nur an einem Log zu erkennen, das nicht mehr wächst und nie
# einen Spielzustand gemeldet hat. Genau darauf wartet dieser Wächter — statt
# dass man von Hand stop_vry.bat und start_vry.bat hinterherschieben muss.

STALL_SETTLE = 20.0       # so lange darf ein frischer Start ohne Log-Zeile bleiben
STALL_AFTER = 25.0        # so lange darf das Log danach stillstehen
STALL_COOLDOWN = 45.0     # Mindestabstand zweier Entstockungs-Neustarts
_stall = {"path": None, "lines": -1, "since": 0.0, "last": 0.0}


def _vry_stalled(now, view):
    """Haengt vry.exe im Start fest?

    Zwei Bedingungen zusammen — einzeln waere jede harmlos:
      1. Das Log waechst nicht mehr.
      2. vry.exe hat noch NIE einen Spielzustand gemeldet.
    Nach dem Start ist ein ruhiges Log voellig normal (Menue ohne Ereignisse),
    darum zaehlt Punkt 2: fehlt der Zustand, ist vry.exe nie fertig geworden.
    """
    path, lines = view.get("log"), view.get("lines", 0)
    if not path:
        return False
    if path != _stall["path"] or lines != _stall["lines"]:
        _stall.update({"path": path, "lines": lines, "since": now})
        return False
    if view.get("state"):
        return False
    return now - _stall["since"] >= STALL_AFTER


def _maybe_unstick(now):
    """Startet ein stumm haengendes vry.exe neu."""
    with _proc_lock:
        if not _proc["desired"] or _proc["giveup"] or _proc["armed"]:
            return
        if now - _proc["last_start"] < max(START_GRACE, STALL_SETTLE):
            return              # frisch gestartet -> es darf noch anlaufen
    if now - _stall["last"] < STALL_COOLDOWN:
        return
    # Fehlt die Region noch im Spiel-Log, wartet vry.exe zu Recht — ein
    # Neustart würde nur erneut ins selbe Loch laufen.
    if not region_ready() or not vry_running():
        return
    if not _vry_stalled(now, _vry_view()):
        return

    _stall.update({"path": None, "lines": -1, "since": 0.0, "last": now})
    _kill_all_vry()
    time.sleep(1.0)
    res = start_vry(force=True)
    with _proc_lock:
        _proc["note"] = ("RankYoinker blieb beim Start haengen — automatisch neu gestartet."
                         if res.get("ok") else
                         "Neustart fehlgeschlagen: %s" % res.get("error", "unbekannt"))


def _resync_allowed(now):
    """Darf gerade ueberhaupt neu gestartet werden?"""
    with _proc_lock:
        if not _proc["desired"] or _proc["giveup"] or _proc["armed"]:
            return False
        if now - _proc["last_start"] < max(START_GRACE, START_SETTLE):
            return False        # frisch gestartet -> es darf noch laden
        if now - _sync["last"] < SYNC_COOLDOWN:
            return False
        return vry_running()    # laeuft nichts, ist der Watchdog zustaendig


def _maybe_resync(state, mid, now):
    """Startet vry.exe neu, wenn es den aktuellen Zustand verpasst hat."""
    if not _resync_allowed(now):
        _sync["mismatch"] = 0.0
        return

    view = _vry_view()
    seen = (view.get("state") or "").upper()
    # 1. vry hängt in der Riot-Retry-Schleife, obwohl das Spiel läuft
    stuck = view.get("phase") == "waiting_riot"
    # 2. Riot ist in einem Match, vry zeigt nachweislich etwas anderes
    behind = state in ("PREGAME", "INGAME") and seen and seen != state
    if not (stuck or behind):
        _sync["mismatch"] = 0.0
        return

    # Erst wenn der Widerspruch anhält — ein einzelner Tick während eines
    # Zustandswechsels ist völlig normal.
    if not _sync["mismatch"]:
        _sync["mismatch"] = now
        return
    if now - _sync["mismatch"] < STALE_AFTER:
        return
    # Pro Match genau ein Nachhol-Neustart. Hängt vry dagegen in der
    # Retry-Schleife, ist ein weiterer Versuch nach der Abklingzeit richtig.
    if _sync["match"] == (mid or state) and not stuck:
        return

    _sync["mismatch"] = 0.0
    _sync["match"] = mid or state
    _sync["last"] = now
    _kill_all_vry()
    time.sleep(1.0)
    res = start_vry(force=True)
    with _proc_lock:
        if not res.get("ok"):
            _proc["note"] = ("Neustart fehlgeschlagen: %s"
                             % res.get("error", "unbekannt"))
        elif stuck:
            _proc["note"] = ("RankYoinker erreichte den Riot Client nicht — neu gestartet, "
                             "das Spiel laeuft ja.")
        else:
            _proc["note"] = ("Laufendes Match erkannt (%s) — RankYoinker wurde neu gestartet, "
                             "damit es geladen wird." % state)


# ---- Spielerkarten vorladen ----
# vry.exe braucht nach dem Matchstart noch ein paar Sekunden (Namen, Loadouts,
# Raenge); erst danach zeichnet die Seite die Karten und fragt "Letzte 5
# Matches" und Partys an. Der Dienst kennt den Kader aber schon vorher - also
# holt er die Daten gleich selbst. Die Seite bekommt sie dann aus dem Cache bzw.
# haengt sich an die laufende Abfrage (_single_flight). Nur wenn die Seite
# offen ist bzw. kuerzlich war (/status kommt im 1,5-s-Takt): ohne Zuschauer
# waeren das rund 60 Riot-Anfragen pro Match fuer nichts. Bis 2.5.0-dev17 galten
# 120 s - live (2026-10-15) kamen die Gegner-"Letzte 5" dadurch erst ~3 min nach
# Matchbeginn bzw. gar nicht: Liegt das Spiel im Vordergrund, drosselt oder
# friert der Browser (Opera GX) den Hintergrund-Tab ein, oder der Tab ist zu und
# wird erst mitten im Match geoeffnet - dann fehlten die Daten beim Hinsehen.
# Jetzt: Seite in den letzten 2 h benutzt = Spielsitzung mit Overlay. Die Anfragen
# laufen im gedrosselten "Letzte 5"-Takt mit niedrigster Prioritaet.
PREFETCH_PAGE_IDLE = 2 * 3600
PREFETCH_BASICS_WAIT = 10.0
_page_seen = {"ts": 0.0}
_prefetch_done = {"key": None, "at": 0.0, "n": 0}


def _prefetch_basics(roster):
    """Agentenauswahl: Namen aller Spieler und Raenge der Gegner VOR Party-Erkennung
    und "Letzte 5". Dieselben Abfragen, die /api/pregame ohnehin stellt -
    laufende werden nicht doppelt gestartet (_in_background). Live 2026-10-15
    lief die Party-Erkennung los, bevor die Seite die Gegner-Raenge angestossen hatte."""
    ids = [p["puuid"] for p in roster]
    deadline = time.time() + PREFETCH_BASICS_WAIT

    def wait(kind, keys):
        while time.time() < deadline:
            with _bg_pending_lock:
                if not any((kind, k) in _bg_pending for k in keys):
                    return
            time.sleep(0.1)

    names_for_nonblocking(ids)
    wait("names", ids)
    names, _ = _names_cached(ids, time.time())
    enemies = [p["puuid"] for p in roster if p.get("team") == "enemy" and names.get(p["puuid"])]
    for u in enemies:
        mmr_summary_nonblocking(u)
    wait("mmr", enemies)


def _prefetch_match(state, mid):
    if not mid or time.time() - _page_seen["ts"] > PREFETCH_PAGE_IDLE:
        return
    key = "%s:%s" % (state, mid)
    # Agentenauswahl: die Gegner tauchen (wenn ueberhaupt) oft erst nach ein paar
    # Sekunden auf - solange der Kader nicht voll ist, weiter nachsehen
    if _prefetch_done["key"] == key and (state == "INGAME" or _prefetch_done["n"] >= 10):
        return
    if state == "INGAME":
        if _game_roster["match"] != mid:
            return                            # Kader noch nicht da -> naechste Runde
        # Gegner zuerst: die eigene Seite liegt meist schon aus der Agentenauswahl vor
        puuids = [p["puuid"] for p in sorted(_game_roster["players"], key=lambda p: bool(p.get("ally")))]
    else:
        try:
            _, _, roster = _lobby_roster()
        except Exception:
            return
        puuids = [p["puuid"] for p in roster]
    if not puuids or (_prefetch_done["key"] == key and len(puuids) <= _prefetch_done["n"]):
        return
    _prefetch_done.update({"key": key, "at": time.time(), "n": len(puuids)})

    def run():
        if state == "PREGAME":
            try:
                _prefetch_basics(roster)
            except Exception:
                pass
        # Dann die Party-Erkennung (eigener Thread): seit dev17 warten die "Letzte 5"
        # auf sie (_pd_bulk_wait) statt umgekehrt - schon ab hier (_pd_party), nicht
        # erst ab ihrer ersten Riot-Anfrage
        with _pd_count_lock:
            _pd_party["n"] += 1

        def party():
            try:
                premades()
            except Exception:
                pass
            finally:
                with _pd_count_lock:
                    _pd_party["n"] -= 1
        threading.Thread(target=party, daemon=True).start()
        # Nacheinander: die Riot-Bremse laesst ohnehin nur ~3 Anfragen/s durch,
        # so ist wenigstens eine Karte nach der anderen fertig statt alle zuletzt
        for pu in puuids:
            if _pd_bulk_paused():
                return                        # Riot-Pause: den Rest holt die Seite spaeter selbst
            try:
                card_data(pu)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


# ---- Stand des laufenden Matches ueber einen Neustart retten ----
# Nach einem Update oder Neustart mitten im Match fragte der Dienst fuer alle
# zehn Spieler alles neu bei Riot ab (Karten-Stats, Party-Erkennung,
# meistgespielter Agent, RR-Verlauf) - dutzende pd-Anfragen auf einmal, genau
# der Stoss, der Cloudflare-Sperren ausloest. Seit 2.4.9 liegt der gesammelte
# Stand des LAUFENDEN Matches in DATA_DIR/match-snapshot.json, immer nur fuer
# dieses eine Match (Agentenauswahl und Match haben dieselbe ID). Beim Start
# prueft der Dienst als Erstes, ob die Datei zum aktuellen Match gehoert, und
# uebernimmt sie dann; Abfragen warten so lange (_snapshot_wait). Im Menue wird
# die Datei geloescht. Gegenstueck in vry.exe: src/match_snapshot.py.
MATCH_SNAPSHOT_FILE = os.path.join(DATA_DIR, "match-snapshot.json")
_SNAPSHOT_CACHES = (("stats", _stats_cache), ("mmr", _mmr_cache), ("agents", _agent_history_cache),
                    ("premadeHist", _premade_hist_cache), ("rr", _rr_cache), ("names", _name_cache))
_snapshot = {"match": None, "since": 0.0, "sig": None, "restored": 0}   # restored: fuer /api/gamestate
# Format je Cache: aendert sich, WAS ein Cache-Eintrag bedeutet, hier erhoehen -
# ein Snapshot aus einer aelteren Version (Update mitten im Match) bringt diesen
# Teil dann nicht mit, der Rest bleibt. mmr 2 = Rang mit Act-Uebernahme (2.4.9).
_SNAPSHOT_FMT = {"stats": 1, "mmr": 2, "agents": 1, "premadeHist": 1, "rr": 1, "names": 1, "premades": 1}
_snapshot_ready = threading.Event()


def _snapshot_wait():
    """Vor jeder Spielerdaten-Abfrage: erst den gespeicherten Stand pruefen lassen."""
    _snapshot_ready.wait(6.0)


def _match_current(ts):
    """Eintrag stammt aus dem LAUFENDEN Match -> gilt bis Matchende, ohne TTL.

    Die letzten Matches und Partys der zehn Spieler aendern sich waehrend des
    Matches nicht. Mit den TTLs (Karten 5 min, Party 15 min) fragte ein F5 oder
    Neustart spaeter im Match alles erneut bei Riot ab (seit 2.5.0 nicht mehr)."""
    return (bool(_snapshot["match"]) and _snapshot["match"] == _game.get("matchId")
            and _game.get("state") in ("PREGAME", "INGAME") and ts >= _snapshot["since"])


def _snapshot_clear():
    _snapshot.update(match=None, since=0.0, sig=None, restored=0)
    for path in (MATCH_SNAPSHOT_FILE, MATCH_SNAPSHOT_FILE + ".tmp"):
        try:
            os.remove(path)
        except OSError:
            pass


def _snapshot_restore():
    """Laeuft beim Start als Erstes: gehoert match-snapshot.json zum aktuellen Match?"""
    try:
        snap = _load_json(MATCH_SNAPSHOT_FILE)
        if not snap.get("match"):
            return
        state = mid = None
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if game_ready():
                try:
                    state, mid = _live_state()
                    break
                except Exception:
                    pass
            time.sleep(1.0)
        if state is None:
            return                              # Spiel (noch) nicht erreichbar - Datei bleibt
        if state in ("PREGAME", "INGAME") and mid == snap["match"]:
            now = time.time()
            restored = 0
            fmt = snap.get("fmt") if isinstance(snap.get("fmt"), dict) else {}
            same = lambda name: fmt.get(name, 1) == _SNAPSHOT_FMT[name]
            with _cache_lock:
                for name, cache in _SNAPSHOT_CACHES:
                    if not same(name):
                        continue
                    for key, value in (snap.get(name) or {}).items():
                        # leere/verborgene Namen nicht: sie bekaemen hier einen frischen
                        # Zeitstempel und sperrten die Nachfrage weitere NAME_TTL_HIDDEN
                        # (live 2026-10-15: Update mitten im Match, Bericht mit "Unknown")
                        if name == "names" and (not isinstance(value, str) or _name_hidden(value)):
                            continue
                        cache[key] = (now, value)
                        restored += 1
                pm = snap.get("premades")
                if isinstance(pm, dict) and pm.get("data") and same("premades"):
                    _premade_cache.update({"match": pm.get("match"), "ts": now, "data": pm["data"]})
            _snapshot.update(match=mid, since=float(snap.get("since") or now), sig=None, restored=restored)
        elif state == "MENUS":
            _snapshot_clear()
    except Exception as e:
        report_error("match-snapshot", e)
    finally:
        _snapshot_ready.set()


def _snapshot_tick(state, mid, now):
    """Aus dem Spielzustands-Takt: neues Match -> neuer Stand, Menue -> loeschen,
    sonst Geaendertes wegschreiben."""
    if not _snapshot_ready.is_set():
        return
    if state == "MENUS":
        if _snapshot["match"] or os.path.exists(MATCH_SNAPSHOT_FILE):
            _snapshot_clear()
            _digests_save(force=True)           # Match vorbei: Kurzfassungen gleich sichern
        return
    if state not in ("PREGAME", "INGAME") or not mid:
        return
    if _snapshot["match"] != mid:
        _snapshot_clear()                       # Stand eines anderen Matches gilt nicht mehr
        with _pd_count_lock:
            _pd_counts.clear()
        # Vorladen und Seite koennen ein paar Sekunden vor dem ersten Takt anfangen
        _snapshot.update(match=mid, since=now - GAME_INTERVAL - 2.0, sig=None, restored=0)
    since = _snapshot["since"]
    data, newest, count = {}, 0.0, 0
    with _cache_lock:
        for name, cache in _SNAPSHOT_CACHES:
            part = {}
            for key, value in cache.items():
                if isinstance(value, tuple) and len(value) == 2 and value[0] >= since:
                    part[key] = value[1]
                    newest = max(newest, value[0])
            data[name] = part
            count += len(part)
        pc = _premade_cache
        if pc.get("data") and pc.get("ts", 0) >= since and str(pc.get("match") or "").startswith(mid + "|"):
            data["premades"] = {"match": pc["match"], "data": pc["data"]}
            newest = max(newest, pc["ts"])
            count += 1
    sig = (count, newest)
    if sig == _snapshot["sig"] or not count:
        return
    try:
        payload = dict(data, v=1, fmt=_SNAPSHOT_FMT, match=mid, since=since, savedAt=now)
        tmp = MATCH_SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        os.replace(tmp, MATCH_SNAPSHOT_FILE)
        _snapshot["sig"] = sig
    except Exception as e:
        report_error("match-snapshot", e)


# ============================ Oberflaeche im Browser oeffnen ============================
# Tray, Startmenue/Desktop-Symbol (open_app.vbs -> "vry_log_server.py --open-app")
# und das Oeffnen nach der Installation (setup/open_overlay.bat) oeffnen die
# Oberflaeche als ganz normale Website im Standardbrowser des Nutzers.
# 2.5.0-dev3 bis dev6 oeffneten stattdessen ein eigenes App-Fenster (Edge/Chrome
# mit --app=) - auf Wunsch des Nutzers wieder entfernt (2.5.0-dev7): zu
# eingeschraenkt, und Opera/Firefox koennen es gar nicht, dann kam ungefragt Edge.
HOSTS_FILE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "etc", "hosts")
DETACHED_PROCESS = 0x00000008


def _local_status_ok(port, host="127.0.0.1", timeout=1.5):
    try:
        with urllib.request.urlopen("http://%s:%d/status" % (host, port), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def overlay_url():
    """Kurzadresse http://rankyoinker/, wenn der Installer sie eingetragen hat
    und Port 80 antwortet - sonst http://localhost:1101/."""
    try:
        with open(HOSTS_FILE, encoding="utf-8", errors="replace") as fh:
            has_host = any(re.search(r"(^|\s)rankyoinker(\s|$)", ln.split("#", 1)[0]) for ln in fh)
    except OSError:
        has_host = False
    if has_host and _local_status_ok(80):
        return "http://rankyoinker/"
    return "http://localhost:%d/" % PORT


def open_overlay_in_browser(url=None):
    url = url or overlay_url()
    try:
        webbrowser.open(url)
        return True
    except Exception as e:
        _ulog("Browser oeffnen fehlgeschlagen: %s: %s" % (type(e).__name__, e))
        return False


def _open_app_cli():
    """"vry_log_server.py --open-app" (Startmenue, Desktop, nach der Installation):
    Dienst starten, falls er nicht laeuft, dann die Seite im Standardbrowser oeffnen."""
    if not _local_status_ok(PORT, timeout=1.0):
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=BASE, close_fds=True,
                             creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
        except Exception as e:
            _ulog("Dienst starten fehlgeschlagen: %s: %s" % (type(e).__name__, e))
        for _ in range(24):
            time.sleep(0.5)
            if _local_status_ok(PORT, timeout=0.8):
                break
    return open_overlay_in_browser()


# ============================ Lesezeichen-Symbol ============================
# "Als App installieren" (PWA: Manifest + Service Worker) gab es von 2.5.0-dev2 bis
# dev14 - auf Wunsch des Nutzers wieder entfernt. Geblieben sind das Symbol fuer
# Lesezeichen/"Zum Home-Bildschirm" (apple-touch-icon) und der Start-Link /?d=<Token>
# (siehe do_GET), damit schon angelegte Handy-Verknuepfungen weiter koppeln.
_APP_ICONS = {
    "/apple-touch-icon.png": "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAMAAAAKE/YAAAAAwFBMVEX/S1v/S1r/Slr/Sln/SVn/SVj/SFf/Rlb/RlX+SVj9R1f+RlX9RlX8RlX+RVT9RVT8RVT8RVP7R1b7RVT7RVP7RFPyRVPrQlDeP0zUO0jMOUXDN0LBNkKwMjycLTaHJy9tICdhHSNSGh9QGR5GFxs7FBctEBMeDQ4ZCwwYCw0YCwwXCwwWCwwVCwwWCgsVCgwVCgsUCgsRCQoOCQkMCAgKBwgJCAgJBwgIBwcHBwcHBgcFBgYDBgYDBQYCBQYABQRStEzqAAAD4ElEQVR42u3caXOaUBgFYEUwRLRQSV0SV9RowVjXxi3y//9VSTqTTpPwXgNHx8yc+53LE3m55y4TMuYXbBmiiSaaaKKJJppoookmmmiiiSaaaKKJJppoookmmmiiiSaaaKIvHW2rmqXswlF1gUZf6Ybc9CtlH8ousGhHbw68nti8ar4odWEVS92e2Ic3aOk2EG0VK4NwuRHaKvQMV+rCzbbDldzDfbVgIcvDNmrDmR8IbbRvZQS1ozW30uWBH8zrmo2t6eimT/5MaNPZ4jb+6RYL1eFiIl3vH1qaix493FznMJLuGizvK+W4x+teexvxb/Z33ZwLH/KsUmXwGEj3He272Zj7upl2KJoni2G1UMSHi23Ug/lU/K23zazz4aX67XwmXjpeNXTnFIn4I9MKf4plPZ/U8x+UtVWu3C/lhxS2M655CrTp6t29WNb+elAoWR+MdqrrNv1yyToN2ioof7F9+31ZO9lotJOf0LCWt83ToKNgVNbmtqm9qU07X5/I74L/9O4i5CxPPQq8+9GsUmGwlke7QyfnmqdDHzPe9gznTXzvFeP7oPKZgv48Wp1so8N/A8FzfI8VSVrXHfOUaDVi9rC6+1eh2PhOvHKJHvfBV8X562wNG9+J0Vap3Jfj3N+/OsDxnXyNaOdrijh/feLo+E6xsI3Keucf827B4zvNatzVOqqyfhnF4PGdBv08S5V/wSgvNAcf36n2PRy9vpBr1d819QI8vtNt1rhaSy6Q6Xxc0+DxnXKHyc115Zcx2HrtnQ+O75RoddJNF8spOr7T7uU5emP1IMb5VPybEsV36g3IKOzk1bncEsV3+l1Tx+jJRYuP7/ToKM6H80lCdLL4BuxPR3G+DpKZE8Y3YlP9JtMJE5V10vhGoNWbTuD4hhxf2Eb9l5zU2PjGnLl8V82JsPENOihSzj6h8Q1Cq+f5yPhGHclFKyrFLBUY3yh0FOetvX+m+IahTdfwjo/zdPGNQ6tnqbD4xqFNJ9dYj88S30B0tPg6cpaaNr6R6OdNp41/hvhGov/OUqenj28oWn00iolvLFp9NAqJbzBaPUtFxDcYrT4aRcQ3Gq06GoXENxz9cjQa+zJOloj4xqNfjkZjk3HeQBU0FG3eaLHLmGDTy+HMSLRzfbcax6Kv7ctE63eP8WiDaKKJJppoookmmmiiiSaaaKKJJppoookmmmiiiSaaaKIvC70cxfwn/Mq7WHQj/B33yYD+haLtfL0f84UGr9/Of7tItGlexX8FI4+8DxRtxX9vpHix6HM1ookmmmiiiSaaaKKJJppoookmmmiiiSaaaKKJJppooon+Ou0PmHSCXIpKwHIAAAAASUVORK5CYII=",
}


# ============================ Problem melden (2.5.0) ============================
# Knopf "Problem melden" in den Einstellungen des Overlays: Beschreibung des
# Nutzers plus eine kleine Diagnose (Version, Kanal, Spielzustand, Riot-Pause,
# Fehler dieser Sitzung) und - nur wenn "Logs anhaengen" angehakt ist - das
# Ende des vry-Logs. Tokens/Passwoerter (_redact_log) und Windows-Benutzer-
# namen in Pfaden (_USER_PATH_RE) werden vorher entfernt. Ziel ist das
# admin-only Dashboard (rankyoinker.de, problem-reports.js). Hoechstens ein
# Bericht pro Minute; rankyoinker.de begrenzt zusaetzlich (3/h je Installation).
PROBLEM_REPORT_URL = "https://rankyoinker.de/api/problem-report"
PROBLEM_LOG_LINES = 400
PROBLEM_LOG_MAX = 16000
PROBLEM_MIN_GAP = 60
_problem = {"last": 0.0}
_problem_lock = threading.Lock()


def _scrub_log(text):
    return _USER_PATH_RE.sub(r"\1<user>", _redact_log(str(text)))


def problem_diagnostics(include_log):
    d = {"version": APP_VERSION, "session": _TELEMETRY_SESSION}
    try:
        cfg = _load_json(CONFIG_JSON_FILE) or {}
        d["channel"] = "dev" if str(cfg.get("updateChannel", "stable")).lower() == "dev" else "stable"
    except Exception:
        pass
    try:
        v = sys.getwindowsversion()
        d["os"] = "Windows %s.%s.%s" % (v.major, v.minor, v.build)
    except Exception:
        d["os"] = sys.platform
    for key, fn in (("activeGame", detect_active_game), ("vryRunning", vry_running)):
        try:
            d[key] = fn()
        except Exception:
            pass
    try:
        d["state"] = (game_state() or {}).get("state")
    except Exception:
        pass
    try:
        g = pd_gate_state()
        d["pdGate"] = {"paused": g["paused"], "seconds": g["seconds"]}
    except Exception:
        pass
    d["errors"] = _telemetry_snapshot()
    log = None
    if include_log:
        path = newest_log()
        if path:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()[-PROBLEM_LOG_LINES:]
                log = _scrub_log("".join(lines))[-PROBLEM_LOG_MAX:]
            except OSError as e:
                log = "[Log nicht lesbar: %s]" % type(e).__name__
    return d, log


def problem_report_send(text, contact, include_log):
    text = str(text or "").strip()[:2000]
    contact = str(contact or "").strip()[:100]
    if not text and not include_log:
        return {"ok": False, "error": "empty"}
    with _problem_lock:
        if time.time() - _problem["last"] < PROBLEM_MIN_GAP:
            return {"ok": False, "error": "too_fast"}
        _problem["last"] = time.time()
    diag, log = problem_diagnostics(include_log)
    payload = {"clientId": _get_client_id(), "version": APP_VERSION, "lang": _heartbeat_lang(),
               "text": text, "contact": contact, "diag": diag}
    if log:
        payload["log"] = log
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # Server nimmt hoechstens 40 KB - dann vorne am Log kuerzen (anteilig, Umlaute zaehlen doppelt)
    while len(body) > 38000 and payload.get("log"):
        keep = int(len(payload["log"]) * 38000 / len(body)) - 200
        payload["log"] = payload["log"][-keep:] if keep > 0 else None
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(PROBLEM_REPORT_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "RankYoinker-ProblemReport"})
    try:
        with _urlopen_public(req, timeout=15) as resp:
            j = json.loads(resp.read().decode("utf-8") or "{}")
        record_feature("problemReport")
        return {"ok": bool(j.get("ok")), "id": j.get("id"), "error": j.get("error")}
    except urllib.error.HTTPError as e:
        with _problem_lock:
            _problem["last"] = 0.0     # abgelehnt -> gleich wieder erlauben
        try:
            err = json.loads(e.read().decode("utf-8") or "{}").get("error")
        except Exception:
            err = None
        return {"ok": False, "error": err or ("busy" if e.code == 429 else "http_%d" % e.code)}
    except Exception as e:
        with _problem_lock:
            _problem["last"] = 0.0
        return {"ok": False, "error": "offline", "detail": type(e).__name__}


# ============================ Live-Daten fuers Handy (2.5.3) ============================
# vry.exe schickt Spieler-Daten per WebSocket (Port 1100). Bis 2.5.2 lauschte der im
# ganzen Netz ohne Anmeldung - jede Webseite und jedes Geraet im WLAN konnte Daten und
# Ingame-Chat mitlesen. Seit 2.5.3 nur noch 127.0.0.1 mit Origin-Pruefung; vry.exe legt
# Version + letzten Heartbeat zusaetzlich in DATA_DIR/live.json ab (src/server.py), und
# das Handy holt sie hier ueber /api/live (gekoppelt, Long-Polling).
LIVE_FILE = os.path.join(DATA_DIR, "live.json")
LIVE_MAX_WAIT = 20.0
_live_running = {"at": 0.0, "v": False}


def _live_vry_running():
    now = time.time()
    if now - _live_running["at"] > 3.0:
        _live_running.update(at=now, v=vry_running())
    return _live_running["v"]


def live_state(since, wait):
    deadline = time.time() + max(0.0, min(float(wait or 0), LIVE_MAX_WAIT))
    while True:
        d = _load_json(LIVE_FILE) or {}
        seq = d.get("seq") if isinstance(d.get("seq"), int) else 0
        if seq != since or time.time() >= deadline:
            return {"ok": True, "seq": seq, "running": _live_vry_running(),
                    "version": d.get("version"), "heartbeat": d.get("heartbeat")}
        time.sleep(0.25)


# ============================ Bildspeicher (2.5.1) ============================
# Grosse Bilder (Kartenbilder, Agenten-Portraits, Champion-Splashs) holt das Overlay
# ueber /img?u=<Adresse> vom Dienst statt direkt vom CDN: gleiche Herkunft wie die
# Seite (kein CORS - live fehlten im Teilen-Banner Karte und Agent, weil
# media.valorant-api.com die Freigabe nur mit Origin und ohne "Vary: Origin" schickt),
# auf der Platte zwischengespeichert (schneller, offline, das Handy laedt uebers
# Heimnetz). Nur diese drei Server, nur https, nur echte PNG/JPEG/WEBP/GIF (Magic
# Bytes) - kein Umweg ins Internet, kein SVG (koennte Skript enthalten).
IMG_HOSTS = ("media.valorant-api.com", "cdn.communitydragon.org", "raw.communitydragon.org")
IMG_CACHE_DIR = os.path.join(DATA_DIR, "img-cache")
IMG_CACHE_MAX = 300 * 1024 * 1024
IMG_MAX_BYTES = 15 * 1024 * 1024
IMG_URL_MAX = 400
_img_sem = threading.Semaphore(6)
_img_trim = {"next": 0.0}
# CommunityDragon antwortet ohne Browser-User-Agent mit 403
_IMG_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _img_allowed(u):
    if not isinstance(u, str) or len(u) > IMG_URL_MAX:
        return False
    try:
        p = urllib.parse.urlsplit(u)
        port = p.port
    except ValueError:
        return False
    return (p.scheme == "https" and p.hostname in IMG_HOSTS and not p.username and not p.password
            and port in (None, 443) and not any(c in u for c in "\\\r\n\t "))


def _img_type(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


class _ImgRedirect(urllib.request.HTTPRedirectHandler):
    """Weiterleitungen nur zu den erlaubten Servern (CDragon leitet um)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _img_allowed(newurl):
            raise urllib.error.HTTPError(newurl, 403, "Weiterleitung nicht erlaubt", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _img_open(req, timeout):
    handlers = [_ImgRedirect()]
    try:
        return urllib.request.build_opener(*handlers).open(req, timeout=timeout)
    except urllib.error.URLError as e:
        if _CERTIFI_CTX and isinstance(e.reason, ssl.SSLError):
            return urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CERTIFI_CTX),
                                               *handlers).open(req, timeout=timeout)
        raise


def _img_cache_trim():
    now = time.time()
    if now < _img_trim["next"]:
        return
    _img_trim["next"] = now + 60
    try:
        files = []
        for name in os.listdir(IMG_CACHE_DIR):
            path = os.path.join(IMG_CACHE_DIR, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
        total = sum(f[1] for f in files)
        if total <= IMG_CACHE_MAX:
            return
        for mtime, size, path in sorted(files):          # am laengsten unbenutzt zuerst
            try:
                os.remove(path)
            except OSError:
                continue
            total -= size
            if total <= IMG_CACHE_MAX * 0.8:
                break
    except OSError:
        pass


def img_get(u):
    """(Bytes, Content-Type) fuer eine erlaubte Bildadresse - aus dem Speicher oder
    einmal vom CDN geholt. None, wenn nicht erlaubt oder nicht zu bekommen."""
    if not _img_allowed(u):
        return None
    key = hashlib.sha256(u.encode("utf-8")).hexdigest()
    path = os.path.join(IMG_CACHE_DIR, key)
    try:
        with open(path, "rb") as f:
            data = f.read()
        t = _img_type(data)
        if t:
            try:
                os.utime(path, None)                       # zuletzt benutzt (fuers Aufraeumen)
            except OSError:
                pass
            return data, t
    except OSError:
        pass

    def fetch():
        try:
            with _img_sem:
                req = urllib.request.Request(u, headers={"User-Agent": _IMG_UA, "Accept": "image/*"})
                with _img_open(req, 20) as r:
                    if r.status != 200 or not _img_allowed(r.geturl()):
                        return None
                    data = r.read(IMG_MAX_BYTES + 1)
            if len(data) > IMG_MAX_BYTES:
                return None
            t = _img_type(data)
            if not t:
                return None
            try:
                os.makedirs(IMG_CACHE_DIR, exist_ok=True)
                tmp = "%s.%d.tmp" % (path, threading.get_ident())
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                _img_cache_trim()
            except OSError:
                pass                                       # Platte voll o. Ae.: trotzdem ausliefern
            return data, t
        except Exception:
            return None
    return _single_flight(("img", key), fetch)


# ============================ Teilen-Link (2.5.0-dev21) ============================
# "Teilen" in der Match-Analyse: Das Overlay schickt das gezeichnete Banner (PNG,
# 1600x900) hierher, der Dienst laedt es EINMAL pro Match auf rankyoinker.de hoch
# und merkt sich den Link (share-links.json) - ein zweites Teilen desselben
# Matches zeigt denselben Link ohne neuen Upload. Vorher fragt er, ob der Server
# erreichbar ist und Uploads gerade angenommen werden. Klappt etwas nicht, meldet
# er nur einen Fehlercode; das Overlay kopiert dann das Bild in die Zwischenablage.
# Der Server prueft und schreibt das PNG selbst neu (share-uploads.js) - hier
# nur eine Vorpruefung, damit nichts Unsinniges ueberhaupt rausgeht.
SHARE_STATUS_URL = "https://rankyoinker.de/api/share/status"
SHARE_UPLOAD_URL = "https://rankyoinker.de/api/share/upload"
SHARE_LINKS_FILE = os.path.join(DATA_DIR, "share-links.json")
SHARE_MAX_BYTES = 4 * 1024 * 1024
# Groesster erlaubter POST-Body ueberhaupt (vorher ungebremst: jede Laenge wurde
# erst komplett eingelesen, auch vor der Pruefung, ob der Absender darf)
POST_MAX_BYTES = SHARE_MAX_BYTES + 64 * 1024
SHARE_SIZE = (1600, 900)
SHARE_LINK_MAX_AGE = 29 * 24 * 3600          # Server loescht nach 30 Tagen
SHARE_LINKS_KEEP = 60
SHARE_LINK_RE = re.compile(r"^https://rankyoinker\.de/uploads/[A-Za-z0-9_-]{22}\.png$")
SHARE_MATCH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_share_lock = threading.Lock()


def _share_png_ok(data):
    if not isinstance(data, (bytes, bytearray)) or not 64 <= len(data) <= SHARE_MAX_BYTES:
        return False
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return False
    w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return (w, h) == SHARE_SIZE


def _share_link_alive(url):
    """Gibt es das Bild noch? True/False, None = unklar (dann den Link trotzdem nehmen).
    Mit Zusatz in der Adresse, damit nicht der Cloudflare-Zwischenspeicher antwortet."""
    try:
        req = urllib.request.Request(url + "?check=%d" % int(time.time()), method="HEAD",
                                     headers={"User-Agent": "RankYoinker-Share/" + APP_VERSION})
        with _urlopen_public(req, timeout=4) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:
        return None


def share_upload(data, game, match_id):
    game = "lol" if game == "lol" else "val"
    match_id = str(match_id or "")
    if not SHARE_MATCH_RE.match(match_id) or not _share_png_ok(data):
        return {"ok": False, "error": "invalid"}
    key = "%s:%s" % (game, match_id)
    with _share_lock:                       # Doppelklick -> nicht zweimal hochladen
        links = _load_json(SHARE_LINKS_FILE) or {}
        hit = links.get(key)
        if isinstance(hit, dict) and SHARE_LINK_RE.match(str(hit.get("url") or "")) \
                and time.time() - float(hit.get("at") or 0) < SHARE_LINK_MAX_AGE:
            # im Dashboard geloescht -> nicht den toten Link zeigen, sondern neu hochladen
            if _share_link_alive(hit["url"]) is not False:
                return {"ok": True, "url": hit["url"], "again": True}
            links.pop(key, None)
        ua = {"User-Agent": "RankYoinker-Share/" + APP_VERSION}
        try:
            with _urlopen_public(urllib.request.Request(SHARE_STATUS_URL, headers=ua), timeout=4) as resp:
                st = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception:
            return {"ok": False, "error": "offline"}
        if not st.get("ok") or not st.get("enabled"):
            return {"ok": False, "error": "disabled"}
        req = urllib.request.Request(SHARE_UPLOAD_URL, data=bytes(data), method="POST", headers=dict(ua, **{
            "Content-Type": "image/png", "X-RY-Client": _get_client_id(), "X-RY-Match": key,
            "X-RY-Version": APP_VERSION}))
        try:
            with _urlopen_public(req, timeout=20) as resp:
                j = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8") or "{}").get("error")
            except Exception:
                err = None
            return {"ok": False, "error": err or ("busy" if e.code == 429 else "http_%d" % e.code)}
        except Exception:
            return {"ok": False, "error": "offline"}
        url = str(j.get("url") or "")
        if not j.get("ok") or not SHARE_LINK_RE.match(url):
            return {"ok": False, "error": str(j.get("error") or "invalid")[:20]}
        links[key] = {"url": url, "at": time.time()}
        if len(links) > SHARE_LINKS_KEEP:
            newest = sorted(links.items(), key=lambda kv: -float((kv[1] or {}).get("at") or 0))
            links = dict(newest[:SHARE_LINKS_KEEP])
        _save_json(SHARE_LINKS_FILE, links)
    record_feature("shareLink")
    return {"ok": True, "url": url}


# ============================ Match-Analyse (2.5.0) ============================
# Nach dem Matchende (INGAME -> MENUS) holt der Dienst einmal die kompletten
# Match-Details (eine pd-Anfrage) und rechnet daraus die Auswertung, die das
# Overlay statt der Lobby zeigt, bis man sie wegklickt oder das naechste Match
# beginnt: ACS, ADR, KAST, HS%, First Kills/Deaths, Multikills, Clutches,
# Economy, Rundenverlauf (wie gewonnen, Kaufrunde), Seiten, Highlights und ein
# Rating relativ zur Lobby (1,00 = Durchschnitt), dazu der Vergleich mit dem
# eigenen Schnitt der letzten Matches. Liegt in DATA_DIR/match-report.json und
# uebersteht so auch einen Neustart. Riot stellt die Details oft erst nach
# einigen Sekunden bereit - darum mehrere Versuche.
MATCH_REPORT_FILE = os.path.join(DATA_DIR, "match-report.json")
REPORT_RETRY = 15.0
REPORT_TRIES = 12
_report = {"pending": None, "tries": 0, "next": 0.0, "data": None, "loaded": False, "busy": False}
_report_lock = threading.Lock()
_HALF_BY_QUEUE = {"swiftplay": 4, "spikerush": 3}


def _report_get():
    with _report_lock:
        if not _report["loaded"]:
            _report["loaded"] = True
            data = _load_json(MATCH_REPORT_FILE)
            if data.get("matchId"):
                _report["data"] = data
        return _report["data"]


def _report_current():
    """Gespeicherte Analyse - aber nur, wenn sie zum angemeldeten Konto gehoert
    (nach einem Kontowechsel zeigte die Lobby sonst das Match des anderen Kontos)."""
    data = _report_get()
    if data and data.get("puuid") and _account["puuid"] and data["puuid"] != _account["puuid"]:
        return None
    return data


def match_report_state():
    data = _report_current()
    return {"matchId": data.get("matchId") if data else None, "pending": bool(_report["pending"])}


def _report_supersede(match_id):
    """Ein neues Match beginnt: die alte Analyse verwerfen. Sie gilt nur bis zum
    naechsten Match - scheiterte dessen Analyse, stand sonst wieder die alte da."""
    if not match_id:
        return
    data = _report_get()
    with _report_lock:
        if _report["pending"] and _report["pending"] != match_id:
            _report["pending"] = None          # Analyse des alten Matches kam nicht mehr rechtzeitig
        if not data or data.get("matchId") == match_id:
            return
        _report["data"] = None
    for path in (MATCH_REPORT_FILE, MATCH_REPORT_FILE + ".tmp"):
        try:
            os.remove(path)
        except OSError:
            pass


def _report_schedule(match_id):
    with _report_lock:
        _report.update(pending=match_id, tries=0, next=time.time() + 5.0)


def _report_tick(now):
    with _report_lock:
        mid = _report["pending"]
        if not mid or _report["busy"] or now < _report["next"]:
            return
        _report["busy"] = True

    def run():
        ok = False
        try:
            data = _match_report_build(mid)
            if data:
                _save_json(MATCH_REPORT_FILE, data)
                with _report_lock:
                    _report.update(data=data, loaded=True)
                ok = True
        except Exception as e:
            report_error("match-report", e)
        finally:
            with _report_lock:
                _report["busy"] = False
                if ok or _report["tries"] + 1 >= REPORT_TRIES:
                    if _report["pending"] == mid:
                        _report["pending"] = None
                else:
                    _report["tries"] += 1
                    _report["next"] = time.time() + REPORT_RETRY

    threading.Thread(target=run, daemon=True).start()


def _kill_time(k):
    t = k.get("roundTime")
    if t is None:
        t = k.get("timeSinceRoundStartMillis")
    return t if isinstance(t, (int, float)) else 0


_HOW = {"Elimination": "elim", "Eliminated": "elim", "Detonate": "bomb", "Bomb detonated": "bomb",
        "Defuse": "defuse", "Bomb defused": "defuse", "Surrendered": "surrender"}
_CEREMONY = {"CeremonyAce": "ace", "CeremonyTeamAce": "teamace", "CeremonyClutch": "clutch",
             "CeremonyFlawless": "flawless", "CeremonyThrifty": "thrifty", "CeremonyCloser": "closer"}


def _buy_type(avg_loadout, pistol):
    if pistol:
        return "pistol"
    if avg_loadout < 1600:
        return "eco"
    if avg_loadout < 3400:
        return "half"
    return "full"


def _report_tier_cached(puuid):
    """Ausserhalb von Competitive (Swiftplay, Unrated ...) steht in match-details
    competitiveTier 0 -> aktueller Rang aus dem MMR-Cache des Matches (keine Anfrage)."""
    with _cache_lock:
        hit = _mmr_cache.get(puuid)
    return ((hit[1] or {}).get("rank") or 0) if hit and isinstance(hit[1], dict) else 0


def _match_report_build(match_id):
    status, j = riot_get("pd", "/match-details/v1/matches/" + match_id)
    if status != 200 or not isinstance(j, dict) or not j.get("players"):
        return None
    with _cache_lock:
        _cache_put(_match_cache, match_id, _digest_match(j), MATCH_CACHE_MAX)
        _digest_disk["dirty"] = True
    me = get_auth()["puuid"]
    info = j.get("matchInfo") or {}
    queue = str(info.get("queueID") or info.get("queueId") or "").lower()
    players = [p for p in (j.get("players") or []) if p.get("subject") and p.get("teamId") not in (None, "Neutral")]
    team_of = {p["subject"]: p.get("teamId") for p in players}
    my_team = team_of.get(me)
    if not my_team:
        return None
    rounds = j.get("roundResults") or []
    team_ids = sorted({t for t in team_of.values() if t})
    enemy_team = next((t for t in team_ids if t != my_team), None)
    ffa = not rounds or len(team_ids) > 2

    st = {p["subject"]: {"k": 0, "d": 0, "a": 0, "dmg": 0, "hs": 0, "body": 0, "leg": 0, "kast": 0, "rounds": 0,
                         "fk": 0, "fd": 0, "mk3": 0, "mk4": 0, "ace": 0, "clutches": [], "spent": 0, "loadout": 0,
                         "plants": 0, "defuses": 0} for p in players}
    half = _HALF_BY_QUEUE.get(queue, 12)
    first_atk = None
    for rnd in rounds[:half]:
        planter = rnd.get("bombPlanter")
        if planter in team_of:
            first_atk = team_of[planter]
            break
    if first_atk is None:
        first_atk = "Red" if "Red" in team_ids else (team_ids[0] if team_ids else None)
    other_atk = next((t for t in team_ids if t != first_atk), None)

    timeline, highlights = [], []
    side_won = {"atk": [0, 0], "def": [0, 0]}
    pistol = [0, 0]
    for i, rnd in enumerate(rounds):
        pstats = {ps.get("subject"): ps for ps in (rnd.get("playerStats") or []) if ps.get("subject") in st}
        kills = []
        for ps in pstats.values():
            for k in ps.get("kills") or []:
                kills.append(dict(k, killer=k.get("killer") or ps.get("subject")))
        kills.sort(key=_kill_time)
        alive = {t: {pu for pu in pstats if team_of.get(pu) == t} for t in team_ids}
        died, killed, assisted, traded = {}, collections.Counter(), set(), set()
        clutch = {}
        for k in kills:
            killer, victim, tk = k.get("killer"), k.get("victim"), _kill_time(k)
            if killer in st and team_of.get(killer) != team_of.get(victim):
                killed[killer] += 1
            for a in k.get("assistants") or []:
                assisted.add(a)
            if victim in st:
                died[victim] = (tk, killer)
                for t in alive.values():
                    t.discard(victim)
                for pu, (dt, dk) in list(died.items()):
                    if dk == victim and pu != victim and team_of.get(pu) == team_of.get(killer) and 0 <= tk - dt <= 5000:
                        traded.add(pu)
            for t in team_ids:
                mine = alive.get(t) or set()
                foes = sum(len(v) for tt, v in alive.items() if tt != t)
                if len(mine) == 1 and foes >= 1 and t not in clutch:
                    clutch[t] = (next(iter(mine)), foes)
        if kills:
            fk = kills[0]
            if fk.get("killer") in st:
                st[fk["killer"]]["fk"] += 1
            if fk.get("victim") in st:
                st[fk["victim"]]["fd"] += 1
        winner = rnd.get("winningTeam")
        for pu, ps in pstats.items():
            s = st[pu]
            s["rounds"] += 1
            for d in ps.get("damage") or []:
                if team_of.get(d.get("receiver")) == team_of.get(pu):
                    continue
                s["dmg"] += d.get("damage") or 0
                s["hs"] += d.get("headshots") or 0
                s["body"] += d.get("bodyshots") or 0
                s["leg"] += d.get("legshots") or 0
            eco = ps.get("economy") or {}
            s["spent"] += eco.get("spent") or 0
            s["loadout"] += eco.get("loadoutValue") or 0
            n = killed.get(pu, 0)
            if n >= 5:
                s["ace"] += 1
            elif n == 4:
                s["mk4"] += 1
            elif n == 3:
                s["mk3"] += 1
            if n or pu in assisted or pu not in died or pu in traded:
                s["kast"] += 1
        if rnd.get("bombPlanter") in st:
            st[rnd["bombPlanter"]]["plants"] += 1
        if rnd.get("bombDefuser") in st:
            st[rnd["bombDefuser"]]["defuses"] += 1
        if winner in clutch:
            pu, foes = clutch[winner]
            st[pu]["clutches"].append({"round": i + 1, "vs": foes})

        if ffa:
            continue
        if i < half:
            atk = first_atk
        elif i < 2 * half:
            atk = other_atk
        else:
            atk = first_atk if (i - 2 * half) % 2 == 0 else other_atk
        planter = rnd.get("bombPlanter")
        if planter in team_of:
            atk = team_of[planter]
        my_side = "atk" if atk == my_team else "def"

        def team_buy(t):
            vals = [((pstats[pu].get("economy") or {}).get("loadoutValue") or 0) for pu in pstats if team_of.get(pu) == t]
            return _buy_type(sum(vals) / len(vals) if vals else 0, i in (0, half))

        won = winner == my_team
        side_won[my_side][1] += 1
        if won:
            side_won[my_side][0] += 1
        if i in (0, half):
            pistol[1] += 1
            if won:
                pistol[0] += 1
        timeline.append({
            "n": i + 1,
            "win": "ally" if won else ("enemy" if winner else "none"),
            "how": _HOW.get(rnd.get("roundResultCode") or rnd.get("roundResult") or "", "time"),
            "ceremony": _CEREMONY.get(rnd.get("roundCeremony") or ""),
            "side": my_side,
            "myKills": killed.get(me, 0),
            "myDied": me in died,
            "buy": team_buy(my_team),
            "enemyBuy": team_buy(enemy_team),
            "plant": bool(rnd.get("bombPlanter")),
        })
        if won and killed.get(me, 0) >= 4:
            highlights.append({"type": "ace" if killed[me] >= 5 else "4k", "round": i + 1})
        if winner == my_team and clutch.get(my_team, (None,))[0] == me:
            highlights.append({"type": "clutch", "round": i + 1, "vs": clutch[my_team][1]})
        if won and (rnd.get("roundCeremony") == "CeremonyFlawless"):
            highlights.append({"type": "flawless", "round": i + 1})

    total_rounds = len(rounds)
    out_players = []
    for p in players:
        pu = p["subject"]
        s = st[pu]
        ps_ = p.get("stats") or {}
        rds = s["rounds"] or ps_.get("roundsPlayed") or 0
        k, d, a = ps_.get("kills") or 0, ps_.get("deaths") or 0, ps_.get("assists") or 0
        shots = s["hs"] + s["body"] + s["leg"]
        out_players.append({
            "puuid": pu, "name": ("%s#%s" % (p.get("gameName"), p.get("tagLine"))) if p.get("gameName") else None,
            "team": team_of[pu], "ally": team_of[pu] == my_team, "me": pu == me, "party": p.get("partyId"),
            "agent": p.get("characterId"), "tier": p.get("competitiveTier") or _report_tier_cached(pu),
            "level": p.get("accountLevel"),
            "kills": k, "deaths": d, "assists": a, "score": ps_.get("score") or 0, "rounds": rds,
            "acs": round((ps_.get("score") or 0) / rds, 1) if rds else None,
            "adr": round(s["dmg"] / rds, 1) if rds and not ffa else None,
            "kast": round(s["kast"] / rds * 100) if rds and not ffa else None,
            "hs": round(s["hs"] / shots * 100, 1) if shots else None,
            "kd": round(k / (d or 1), 2),
            "fk": s["fk"], "fd": s["fd"], "mk3": s["mk3"], "mk4": s["mk4"], "ace": s["ace"],
            "clutches": s["clutches"], "plants": s["plants"], "defuses": s["defuses"],
            "econ": round(s["dmg"] / (s["spent"] / 1000.0)) if s["spent"] else None,
            "avgLoadout": round(s["loadout"] / rds) if rds and not ffa else None,
        })

    # Riot liefert in match-details keine Namen mehr (gameName leer, live gesehen
    # 2026-10-15: alle 10 null) -> Namens-Cache, fuer den Rest EINE name-service-Anfrage
    missing = [x["puuid"] for x in out_players if not x["name"]]
    if missing:
        try:
            got = names_for(missing, retry_unknown=True)
        except Exception:
            got = {}
        for x in out_players:
            n = got.get(x["puuid"])
            if not x["name"] and n and not _name_hidden(n):
                x["name"] = n

    def avg(key):
        vals = [x[key] for x in out_players if x.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0

    a_acs, a_adr, a_kast = avg("acs"), avg("adr"), avg("kast")
    kdr = {x["puuid"]: ((x["kills"] / x["rounds"] + 0.1) / (x["deaths"] / x["rounds"] + 0.1)) if x["rounds"] else 0
           for x in out_players}
    a_kdr = sum(kdr.values()) / len(kdr) if kdr else 0
    for x in out_players:
        parts = [(0.35, x["acs"], a_acs), (0.25, kdr[x["puuid"]], a_kdr)]
        if not ffa:
            parts += [(0.2, x["adr"], a_adr), (0.2, x["kast"], a_kast)]
        wsum = sum(w for w, v, m in parts if v is not None and m)
        x["rating"] = round(sum(w * v / m for w, v, m in parts if v is not None and m) / wsum, 2) if wsum else None
    ranked = sorted([x for x in out_players if x["rating"] is not None], key=lambda x: -x["rating"])
    if ranked:
        ranked[0]["mvp"] = "match"
        # Team-MVP = Bester des ANDEREN Teams; im Team des Match-MVP ist er das schon
        # (vorher bekam dort zusaetzlich der Zweitbeste "Team-MVP")
        for t in team_ids:
            best = next((x for x in ranked if x["team"] == t), None)
            if best and best is not ranked[0] and not ffa:
                best["mvp"] = "team"
    out_players.sort(key=lambda x: (not x["ally"], -(x["acs"] or 0)))

    teams = {t.get("teamId"): t for t in (j.get("teams") or [])}
    mine_t, enemy_t = teams.get(my_team) or {}, teams.get(enemy_team) or {}
    if ffa:
        my_row = next((x for x in out_players if x["me"]), {})
        place = 1 + sum(1 for x in out_players if (x["kills"] or 0) > (my_row.get("kills") or 0))
        result = "win" if place == 1 else "loss"
        score = {"ally": my_row.get("kills") or 0, "enemy": None, "place": place, "of": len(out_players)}
    else:
        result = "win" if mine_t.get("won") else ("loss" if enemy_t.get("won") else "draw")
        score = {"ally": mine_t.get("roundsWon") or 0, "enemy": enemy_t.get("roundsWon") or 0}

    me_row = next((x for x in out_players if x["me"]), None)
    if me_row:
        for c in me_row["clutches"]:
            if not any(h["type"] == "clutch" and h["round"] == c["round"] for h in highlights):
                highlights.append({"type": "clutch", "round": c["round"], "vs": c["vs"]})
        if me_row["fk"] >= 4:
            highlights.append({"type": "firstbloods", "n": me_row["fk"]})
        if me_row.get("mvp"):
            highlights.insert(0, {"type": "mvp", "kind": me_row["mvp"]})
    # Hoechstens 8: erst nach Gewicht auswaehlen, dann nach Runde ordnen. Live
    # (2026-10-15, 13:9) fiel sonst der eigene 4K aus Runde 21 hinter drei
    # Team-Flawless heraus.
    if len(highlights) > 8:
        weight = {"mvp": 0, "ace": 1, "clutch": 2, "4k": 3, "firstbloods": 4, "flawless": 5}
        highlights = sorted(highlights, key=lambda h: (weight.get(h["type"], 9), h.get("round") or 0))[:8]
    highlights.sort(key=lambda h: (h["type"] != "mvp", h.get("round") or 0))

    report = {
        "v": 1, "matchId": match_id, "puuid": me, "createdAt": int(time.time()),
        "map": info.get("mapId"), "queue": queue, "ffa": ffa,
        "startedAt": info.get("gameStartMillis"), "durationMs": info.get("gameLengthMillis"),
        "rounds": total_rounds, "result": result, "score": score,
        "players": out_players, "timeline": timeline, "highlights": highlights[:8],
        "sides": None if ffa else {"atk": side_won["atk"], "def": side_won["def"], "pistol": pistol},
        "myAvg": None,
    }
    try:
        hist = player_stats(me, 10, season=False)
        rows = [r for r in (hist.get("matches") or []) if r.get("matchId") != match_id and r.get("rounds")]
        if len(rows) >= 3:
            n = len(rows)
            shots = sum((r.get("headshots") or 0) + (r.get("bodyshots") or 0) + (r.get("legshots") or 0) for r in rows)
            report["myAvg"] = {
                "games": n,
                "acs": round(sum(r.get("score") or 0 for r in rows) / sum(r["rounds"] for r in rows), 1),
                "adr": round(sum(r.get("damage") or 0 for r in rows) / sum(r["rounds"] for r in rows), 1),
                "kd": round(sum(r.get("kills") or 0 for r in rows) / (sum(r.get("deaths") or 0 for r in rows) or 1), 2),
                "hs": round(sum(r.get("headshots") or 0 for r in rows) / shots * 100, 1) if shots else None,
                "winrate": round(sum(1 for r in rows if r.get("won") is True) / n * 100),
            }
    except Exception:
        pass
    return report


def _game_watcher():
    while True:
        time.sleep(GAME_INTERVAL)
        try:
            _pd_vry_blocks_tick()
        except Exception:
            pass
        try:
            _digests_save()
            now = time.time()
            if not game_ready():
                if _game["state"] != "offline":
                    _game.update({"state": "offline", "matchId": None, "since": now})
                _game["ts"] = now
                _sync["mismatch"] = 0.0
                _sync["match"] = None
                _stall.update({"path": None, "lines": -1, "since": 0.0})
                continue
            # Bewusst VOR der Riot-Abfrage: ein stumm hängendes vry.exe soll
            # auch dann wieder hochkommen, wenn gerade keine Token zu holen sind.
            _maybe_unstick(now)
            try:
                state, mid = _live_state()
            except Exception:
                continue        # Token/Region noch nicht da -> spaeter erneut
            if state != _game["state"] or mid != _game["matchId"]:
                if _game["state"] == "INGAME" and _game["matchId"] and state != "INGAME":
                    _report_schedule(_game["matchId"])     # Match vorbei -> Analyse holen
                _game.update({"state": state, "matchId": mid, "since": now})
                _game_hist.append((now, state))
            _game["ts"] = now
            _snapshot_tick(state, mid, now)
            if state in ("PREGAME", "INGAME"):
                _report_supersede(mid)
            _report_tick(now)
            # Begegnungen zählen, sobald der volle Kader steht (INGAME) —
            # unabhängig davon, ob die Seite gerade offen ist
            if state == "INGAME":
                record_encounter_now(mid)
                _notify_match_tracked_once("valorant", mid)
            if state in ("PREGAME", "INGAME"):
                _prefetch_match(state, mid)   # Karten-Daten holen, waehrend vry.exe noch laedt
            _maybe_resync(state, mid, now)
        except Exception:
            pass


# ============================ Kopplung (Handy im LAN) ============================
#
# Ablauf: Der PC erzeugt einen kurzlebigen Kopplungscode und zeigt ihn als
# QR-Code. Das Handy öffnet die Adresse mit ?p=<Code>, löst ihn damit ein und
# bekommt ein dauerhaftes Geräte-Token als Cookie. Der Code ist danach
# verbraucht — ein zweites Handy braucht einen neuen. Entkoppeln löscht das
# Token, das Gerät fliegt sofort raus.

PAIR_FILE = os.path.join(DATA_DIR, "vry_pair.json")
PAIR_COOKIE = "vry_dev"
PAIR_TTL = 300                  # QR-Code laeuft nach 5 Minuten ab
PAIR_MAX_AGE = 60 * 60 * 24 * 365
_pair_lock = threading.Lock()


def _pair_load():
    d = _load_json(PAIR_FILE)
    if not isinstance(d.get("devices"), list):
        d["devices"] = []
    return d


def _pair_save(d):
    _save_json(PAIR_FILE, d)


def _is_local(ip):
    """Kommt die Anfrage vom PC selbst?"""
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip == "::1" or ip.startswith("127.")


def _is_private(ip):
    """Grobe Pruefung auf private IPv4-Bereiche — VPN-Adressen fallen so raus."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def lan_ips():
    """Adressen, unter denen der PC im eigenen Netz erreichbar ist.
    Vorne steht die Adresse der Standardroute — die stimmt fast immer."""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))   # kein echter Verkehr, nur Routenwahl
            out.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and _is_private(ip):
                out.append(ip)
    except Exception:
        pass
    return [ip for ip in out if not ip.startswith("127.")]


def _ua_name(ua):
    """Lesbarer Geraetename aus dem User-Agent — nur fuer die Anzeige."""
    ua = ua or ""
    if "iPhone" in ua:
        dev = "iPhone"
    elif "iPad" in ua:
        dev = "iPad"
    elif "Android" in ua:
        dev = "Android-Handy"
    elif "Macintosh" in ua:
        dev = "Mac"
    elif "Windows" in ua:
        dev = "Windows-PC"
    else:
        dev = "Gerät"
    for label, needle in (("Edge", "Edg/"), ("Chrome", "Chrome/"),
                          ("Firefox", "Firefox/"), ("Safari", "Safari/")):
        if needle in ua:
            return "%s · %s" % (dev, label)
    return dev


def pair_state():
    """Aktueller Stand fuer die Oberflaeche — ohne Tokens."""
    now = time.time()
    with _pair_lock:
        d = _pair_load()
        pend = d.get("pending")
        if pend and pend.get("exp", 0) <= now:
            d.pop("pending", None)
            _pair_save(d)
            pend = None
        devices = [{"id": x.get("id"), "name": x.get("name"), "ip": x.get("ip"),
                    "created": x.get("created"), "seen": x.get("seen")}
                   for x in d["devices"]]
    return {"ok": True, "devices": devices, "ips": lan_ips(), "port": PORT,
            "pending": bool(pend), "expires": (pend or {}).get("exp", 0),
            "url": (pend or {}).get("url", "")}


def pair_start(host=None):
    """Legt einen frischen Kopplungscode an. Ein alter verfaellt damit."""
    ips = lan_ips()
    if host and host not in ips:
        host = None
    host = host or (ips[0] if ips else "")
    if not host:
        return {"ok": False, "error": "Keine Netzwerkadresse gefunden — haengt der PC im WLAN/LAN?"}
    code = secrets.token_urlsafe(9)
    url = "http://%s:%d/?p=%s" % (host, PORT, code)
    with _pair_lock:
        d = _pair_load()
        d["pending"] = {"code": code, "exp": time.time() + PAIR_TTL, "url": url}
        _pair_save(d)
    out = pair_state()
    out["url"] = url
    return out


def pair_cancel():
    with _pair_lock:
        d = _pair_load()
        if d.pop("pending", None) is not None:
            _pair_save(d)
    return pair_state()


def pair_claim(code, ua, ip):
    """Loest den Code ein und liefert das Geraete-Token — oder None."""
    if not code:
        return None
    now = time.time()
    with _pair_lock:
        d = _pair_load()
        pend = d.get("pending")
        if not pend or pend.get("exp", 0) <= now:
            return None
        if not hmac.compare_digest(str(pend.get("code") or ""), str(code)):
            return None
        d.pop("pending", None)                      # Code ist verbraucht
        token = secrets.token_urlsafe(24)
        d["devices"].append({"id": secrets.token_urlsafe(6), "token": token,
                             "name": _ua_name(ua), "ip": ip,
                             "created": now, "seen": now})
        _pair_save(d)
    record_feature("phone_paired")
    return token


def pair_check(token, ip):
    """Prueft das Geraete-Token und schreibt den letzten Zugriff mit."""
    if not token:
        return False
    now = time.time()
    with _pair_lock:
        d = _pair_load()
        for dev in d["devices"]:
            if hmac.compare_digest(str(dev.get("token") or ""), str(token)):
                # Nicht bei jedem Abruf schreiben — die Seite pollt im Sekundentakt
                if now - (dev.get("seen") or 0) > 60 or dev.get("ip") != ip:
                    dev["seen"] = now
                    dev["ip"] = ip
                    _pair_save(d)
                return True
    return False


def pair_revoke(dev_id=None, all_devices=False):
    with _pair_lock:
        d = _pair_load()
        if all_devices:
            d["devices"] = []
        else:
            d["devices"] = [x for x in d["devices"] if x.get("id") != dev_id]
        d.pop("pending", None)
        _pair_save(d)
    return pair_state()


# ============================ League of Legends (LCU) ============================
# Eigener, komplett unabhängiger Zweig für League of Legends — nutzt NICHT die
# VALORANT-Tokens von oben. League hat kein Lockfile an fester Stelle (das
# Installationsverzeichnis kann überall liegen); Port und Token für die
# lokale "LCU"-API stehen stattdessen in der Kommandozeile von
# LeagueClientUx.exe (--app-port, --remoting-auth-token) — so machen es alle
# LCU-Tools, das ist der übliche Weg.
#
# Während eine Partie WIRKLICH läuft, kommen die Live-Daten (Gold, Items,
# KDA aller Spieler) nicht von der LCU, sondern von einer zweiten, komplett
# unauthentifizierten API auf Port 2999 ("Live Client Data API") — die gibt es
# nur, solange das Spiel selbst läuft.
#
# vry.exe (das externe Programm) kennt League gar nicht — alles hier ist
# reines Python gegen diese beiden lokalen APIs, ohne Riot-Entwickler-Key.

LOL_UX_PROC = "LeagueClientUx.exe"        # der Client (Lobby, Champ Select, ...)
LOL_GAME_PROC = "League of Legends.exe"   # das eigentliche Spiel (nur waehrend INGAME)


def lcu_client_present():
    return _image_running(LOL_UX_PROC)


def league_present():
    """Client ODER Spiel offen — fuer die Spiel-Erkennung reicht das."""
    return lcu_client_present() or _image_running(LOL_GAME_PROC)


class LcuError(Exception):
    pass


_lcu_lock = threading.Lock()
_lcu = {"ts": 0.0, "port": None, "headers": None}
LCU_AUTH_TTL = 20.0     # Client-Neustart aendert Port+Token -> nicht ewig cachen


def _lcu_detect():
    """Port + Token aus der Kommandozeile von LeagueClientUx.exe lesen."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
          "Select-Object -First 1 -ExpandProperty CommandLine" % LOL_UX_PROC)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=10,
                             creationflags=CREATE_NO_WINDOW).stdout or ""
    except Exception:
        return None
    port_m = re.search(r"--app-port=(\d+)", out)
    token_m = re.search(r"--remoting-auth-token=([\w-]+)", out)
    if not (port_m and token_m):
        return None
    basic = base64.b64encode(("riot:" + token_m.group(1)).encode()).decode()
    return {"port": port_m.group(1),
            "headers": {"Authorization": "Basic " + basic,
                        "Content-Type": "application/json"}}


def lcu_auth(force=False):
    with _lcu_lock:
        if not force and _lcu["port"] and time.time() - _lcu["ts"] < LCU_AUTH_TTL:
            return dict(_lcu)
        info = _lcu_detect()
        if not info:
            raise LcuError("League Client laeuft nicht (oder noch nicht bereit).")
        _lcu.update(info)
        _lcu["ts"] = time.time()
        return dict(_lcu)


_LCU_ERR_PATH = os.path.join(DATA_DIR, "rankyoinker-lcu-error.txt")


def _note_lcu_error(url, exc):
    # lcu_req() turns every failure into the same generic LcuError for the UI;
    # keep the real cause on disk so a broken LCU connection is diagnosable.
    report_error("lcu", exc)
    try:
        with open(_LCU_ERR_PATH, "w", encoding="utf-8") as f:
            f.write("%s: %s -> %r\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), url, exc))
    except OSError:
        pass


def lcu_req(method, path, body=None, _retry=True):
    auth = lcu_auth()
    url = "https://127.0.0.1:%s%s" % (auth["port"], path)
    data = json.dumps(body) if body is not None else None
    try:
        status, j = _http(url, auth["headers"], method=method, body=data, local_unverified=True)
    except Exception as e:
        _note_lcu_error(url, e)
        raise LcuError("League Client nicht erreichbar.")
    if status in (401, 403) and _retry:
        lcu_auth(force=True)
        return lcu_req(method, path, body, _retry=False)
    return status, j


def lcu_get(path):
    return lcu_req("GET", path)


def lcu_post(path, body=None):
    return lcu_req("POST", path, body if body is not None else {})


def lcu_put(path, body=None):
    return lcu_req("PUT", path, body if body is not None else {})


def lcu_delete(path):
    return lcu_req("DELETE", path)


def lcu_patch(path, body=None):
    return lcu_req("PATCH", path, body if body is not None else {})


# ---- Spielzustand (Lobby/Queue/ReadyCheck/ChampSelect/Ingame) ----

LOL_PHASE_STATE = {
    "Lobby": "LOBBY", "Matchmaking": "QUEUE", "ReadyCheck": "READYCHECK",
    "ChampSelect": "CHAMPSELECT", "GameStart": "INGAME", "InProgress": "INGAME",
    "Reconnect": "INGAME", "WaitingForStats": "POSTGAME",
    "PreEndOfGame": "POSTGAME", "EndOfGame": "POSTGAME",
}


def lol_gamestate():
    """Echter League-Zustand, roh von der LCU-API — unabhaengig davon, ob die
    Seite gerade offen ist."""
    if not league_present():
        return {"ok": False, "phase": None, "state": "OFFLINE"}
    try:
        status, phase = lcu_get("/lol-gameflow/v1/gameflow-phase")
    except LcuError:
        return {"ok": False, "phase": None, "state": "OFFLINE"}
    if status != 200:
        return {"ok": False, "phase": None, "state": "OFFLINE"}
    phase = phase if isinstance(phase, str) else None
    return {"ok": True, "phase": phase,
            "state": LOL_PHASE_STATE.get(phase, "LOBBY" if phase else "NONE")}


# ---- Welches Spiel ist gerade "aktiv"? (für die automatische Umschaltung) ----
# Ein laufendes Match/Queue schlägt einen bloss geöffneten Client. Sind beide
# gleich weit (meistens: beide nur im Menü offen), bleibt die zuletzt gezeigte
# Ansicht stehen, statt bei jedem Tick hin- und herzuspringen — die Seite bietet
# daneben einen manuellen Umschalter für genau diesen Fall.

_active_game = {"game": None}


def _valorant_score():
    if not valorant_present():
        return 0
    return 3 if game_state().get("state") in ("INGAME", "PREGAME") else 1


def _league_score():
    if not league_present():
        return 0
    return 3 if lol_gamestate().get("state") in ("READYCHECK", "CHAMPSELECT", "INGAME", "QUEUE") else 1


def detect_active_game():
    """Bewusst vorsichtig: Ein Match/eine Queue (Score 3) schlaegt einen bloss
    geoeffneten Client (Score 1) sofort. Sind beide gleich weit — meistens:
    beide nur im Menue offen, weil viele Leute beide Clients nebenbei laufen
    lassen — wird NICHT geraten. Frueher fiel ein Gleichstand ohne Vorwissen
    immer auf Valorant zurueck; das sah nach kaputter Erkennung aus, sobald
    League eigentlich gemeint war. Jetzt bleibt es in dem Fall unentschieden
    (game: None) und die Seite zeigt den manuellen Umschalter — die einzige
    ehrliche Antwort, wenn beide Spiele gleich "aktiv" wirken."""
    sv, sl = _valorant_score(), _league_score()
    if sv == 0 and sl == 0:
        _active_game["game"] = None
    elif sv != sl:
        _active_game["game"] = "valorant" if sv > sl else "league"
    # sonst (sv == sl > 0): echter Gleichstand, meistens beide Clients nur im
    # Menü offen -> nicht raten. Ein vorheriger Stand (aus einem früheren
    # Ungleichstand) bleibt einfach stehen, statt auf Valorant zu springen.
    return {"game": _active_game["game"],
            "valorantPresent": valorant_present(), "valorantScore": sv,
            "leaguePresent": league_present(), "leagueScore": sl}


# ---- Eigener Summoner ----

_lol_self = {"ts": 0.0, "data": None}
LOL_SELF_TTL = 30.0


def lol_current_summoner():
    now = time.time()
    if _lol_self["data"] and now - _lol_self["ts"] < LOL_SELF_TTL:
        return _lol_self["data"]
    status, j = lcu_get("/lol-summoner/v1/current-summoner")
    if status != 200 or not j:
        raise LcuError("Kein Summoner gefunden (Status %s)." % status)
    _lol_self["data"] = j
    _lol_self["ts"] = now
    return j


def lol_session():
    s = lol_current_summoner()
    return {"ok": True, "puuid": s.get("puuid"), "summonerId": s.get("summonerId"),
            "name": s.get("gameName") or s.get("displayName"),
            "tagLine": s.get("tagLine"), "level": s.get("summonerLevel"),
            "iconId": s.get("profileIconId")}


# ---- Rang ----

RANKED_QUEUES_SHOWN = ("RANKED_SOLO_5x5", "RANKED_FLEX_SR")


def lol_ranked(puuid):
    """Rang je Ranked-Warteschlange (Solo/Duo + Flex) fuer eine puuid — funktioniert
    auch fuer Mitspieler, nicht nur den eigenen Account."""
    try:
        status, j = lcu_get("/lol-ranked/v1/ranked-stats/%s" % puuid)
    except LcuError:
        return {}
    if status != 200 or not j:
        return {}
    out = {}
    for q in (j.get("queues") or []):
        qt = q.get("queueType")
        if qt not in RANKED_QUEUES_SHOWN:
            continue
        tier = q.get("tier") or ""
        # Peak der Vorsaison: die LCU liefert getrennt "höchster je erreichter
        # Rang" und "Rang beim Saisonende" — der höchste ist der eigentliche
        # Peak, fällt aber auf den Endstand zurück, falls er mal fehlt.
        prev_tier = q.get("previousSeasonHighestTier") or q.get("previousSeasonEndTier")
        prev_div = q.get("previousSeasonHighestDivision") or q.get("previousSeasonEndDivision")
        out[qt] = {
            "tier": tier or None, "division": q.get("division") or None,
            "lp": q.get("leaguePoints"), "wins": q.get("wins"), "losses": q.get("losses"),
            "provisional": bool(q.get("isProvisional")),
            "prevSeason": {"tier": prev_tier, "division": prev_div} if prev_tier else None,
        }
    return out


# ---- Warteschlangen-Namen (für die Queue-Auswahl) ----

_lol_queues = {"ts": 0.0, "map": {}, "list": []}


def _lol_queue_catalog():
    now = time.time()
    if now - _lol_queues["ts"] < 86400 and _lol_queues["list"]:
        return _lol_queues
    try:
        status, j = lcu_get("/lol-game-queues/v1/queues")
        if status == 200 and isinstance(j, list):
            items = [q for q in j
                     if q.get("id", -1) > 0 and q.get("gameMode") not in (None, "TUTORIAL")
                     and q.get("shortName")]
            _lol_queues["map"] = {q["id"]: q.get("shortName") or q.get("description") for q in items}
            _lol_queues["list"] = sorted(
                [{"id": q["id"], "name": q.get("shortName") or q.get("description")}
                 for q in items], key=lambda q: q["id"])
            _lol_queues["ts"] = now
    except Exception:
        pass
    return _lol_queues


def lol_queue_list():
    return {"ok": True, "queues": _lol_queue_catalog()["list"]}


# ---- Lobby / Party (Menü) ----

def lol_lobby():
    try:
        status, j = lcu_get("/lol-lobby/v2/lobby")
    except LcuError as e:
        return {"ok": False, "error": str(e)}
    if status != 200 or not j:
        return {"ok": False, "error": "Keine Lobby offen."}
    names = _lol_queue_catalog()["map"]
    cfg = j.get("gameConfig") or {}
    qid = cfg.get("queueId")
    raw_members = j.get("members") or []
    puuids = [m.get("puuid") for m in raw_members if m.get("puuid")]
    ranks = dict(zip(puuids, _pool.map(lol_ranked, puuids))) if puuids else {}
    last_champs = dict(zip(puuids, _pool.map(lol_last_champion, puuids))) if puuids else {}
    # Die Lobby liefert gameName/summonerName mittlerweile leer (Riot hat das
    # aus Datenschutzgründen entfernt) - genau wie in der Champion-Auswahl
    # muss der Name über die summonerId nachgeschlagen werden.
    resolved = _lol_resolve_names([m.get("summonerId") for m in raw_members])
    members = []
    for m in raw_members:
        r = ranks.get(m.get("puuid")) or {}
        info = resolved.get(m.get("summonerId")) or {}
        members.append({
            "puuid": m.get("puuid"), "summonerId": m.get("summonerId"),
            "name": info.get("name") or m.get("gameName") or m.get("summonerName") or "—",
            "tagLine": info.get("tagLine") or m.get("tagLine"),
            "leader": bool(m.get("isLeader")), "ready": bool(m.get("ready")),
            "position": m.get("firstPositionPreference") or None,
            "positionSecondary": m.get("secondPositionPreference") or None,
            "rankSolo": r.get("RANKED_SOLO_5x5"), "rankFlex": r.get("RANKED_FLEX_SR"),
            "lastChampionId": last_champs.get(m.get("puuid")),
        })
    return {"ok": True, "queueId": qid, "queueName": names.get(qid),
            "canStart": bool(j.get("canStartActivity", True)), "members": members}


_lol_queue_clock = {"start": None}


def _lol_queue_elapsed(searching, lcu_secs):
    """Sekunden in der Warteschlange. Der Dienst führt eine eigene Uhr (Start
    einmal gemerkt) und gleicht sie nur bei mehr als 2 s Abweichung an die LCU
    an - z. B. wenn das Overlay erst mitten in der Suche aufging oder eine neue
    Suche begann. Fehlt die Angabe der LCU, zählt die eigene Uhr ab dem ersten Sehen."""
    now = time.monotonic()
    if not searching:
        _lol_queue_clock["start"] = None
        return None
    cand = now - lcu_secs if _is_num(lcu_secs) and lcu_secs >= 0 else None
    start = _lol_queue_clock["start"]
    if start is None:
        start = cand if cand is not None else now
    elif cand is not None and abs(cand - start) > 2:
        start = cand
    _lol_queue_clock["start"] = start
    return round(max(0.0, now - start), 2)


def lol_search_state():
    try:
        status, j = lcu_get("/lol-lobby/v2/lobby/matchmaking/search-state")
    except LcuError:
        status, j = None, None
    if status != 200 or not j:
        _lol_queue_elapsed(False, None)
        return {"ok": True, "searching": False}
    searching = j.get("searchState") == "Searching"
    est, secs = j.get("estimatedQueueTime"), j.get("timeInQueue")
    if searching:
        # search-state nennt nur den Zustand; Wartezeit und Schätzung stehen in
        # /lol-matchmaking/v1/search. Bis 2.4.7 kam darum immer 0 an und die
        # Uhr im Overlay sprang bei jeder Abfrage auf 0:00 zurück.
        try:
            st2, mm = lcu_get("/lol-matchmaking/v1/search")
        except LcuError:
            st2, mm = None, None
        if st2 == 200 and isinstance(mm, dict):
            est = mm.get("estimatedQueueTime", est)
            secs = mm.get("timeInQueue", secs)
    return {"ok": True, "searching": searching,
            "estimatedSeconds": est if _is_num(est) and est > 0 else None,
            "timeInQueue": _lol_queue_elapsed(searching, secs),
            "readyCheck": j.get("lowPriorityData") is not None}


POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY", "FILL", "UNSELECTED")


def lol_queue_action(action, payload=None):
    payload = payload or {}
    body = None
    try:
        if action == "setQueue":
            qid = payload.get("queueId")
            if not qid:
                return {"ok": False, "error": "Keine Queue angegeben."}
            status, body = lcu_post("/lol-lobby/v2/lobby", {"queueId": int(qid)})
        elif action == "roles":
            primary = payload.get("primary") if payload.get("primary") in POSITIONS else "UNSELECTED"
            secondary = payload.get("secondary") if payload.get("secondary") in POSITIONS else "UNSELECTED"
            status, body = lcu_put("/lol-lobby/v2/lobby/members/localMember/position-preferences",
                                {"firstPreference": primary, "secondPreference": secondary})
        elif action == "search":
            # Ranked-Warteschlangen (Solo/Duo, Flex) lehnen die Matchmaking-Suche
            # ab, wenn keine Positionspräferenz gesetzt ist. Die Lobby wird hier
            # per API angelegt (setQueue) statt über den Client-Dialog, der
            # normalerweise zur Rollenwahl zwingt - darum blieb das bisher auf
            # UNSELECTED/UNSELECTED stehen und die Suche schlug fehl. Das UI
            # schickt die vom Spieler gewählte(n) Lane(s) (Standard: FILL/FILL)
            # jetzt per "roles"-Aktion IMMER direkt vor dem Klick auf "Queue
            # starten" - hier also nichts mehr raten oder überschreiben.
            status, body = lcu_post("/lol-lobby/v2/lobby/matchmaking/search")
        elif action == "cancel":
            status, body = lcu_delete("/lol-lobby/v2/lobby/matchmaking/search")
        elif action == "leave":
            status, body = lcu_delete("/lol-lobby/v2/lobby")
        else:
            return {"ok": False, "error": "Unbekannte Aktion."}
    except LcuError as e:
        return {"ok": False, "error": str(e)}
    if status in (200, 201, 204):
        return {"ok": True}
    # Der League Client liefert bei Ablehnung meist eine JSON-Fehlermeldung
    # mit - die wurde bisher verworfen (nur der Status-Code landete in der
    # Antwort), darum sah jeder Fehler wie ein bedeutungsloses "Unbekannter
    # Fehler." aus statt den tatsächlichen Grund zu zeigen.
    msg = None
    if isinstance(body, dict):
        msg = body.get("message") or body.get("errorCode")
    return {"ok": False, "error": msg or ("League Client lehnte ab (Status %s)." % status)}


# ---- Ready-Check (automatisches Annehmen) ----
# Gleiches Prinzip wie der VALORANT-Instalock oben: Der Dienst tickt selbst im
# Hintergrund, damit ein gedrosselter/geschlossener Tab das Annehmen nicht
# verpasst. Anders als beim Instalock gibt es hier keinen Zielzustand zum
# Konfigurieren — nur ein Ein/Aus, das über Neustarts hinweg gilt.

LOL_FILE = os.path.join(DATA_DIR, "vry_lol.json")
_lol_cfg_lock = threading.Lock()
_lol_cfg = {"autoAccept": True, "autoBuild": True}


def _lol_cfg_restore():
    data = _load_json(LOL_FILE)
    with _lol_cfg_lock:
        _lol_cfg["autoAccept"] = bool(data.get("autoAccept", True))
        _lol_cfg["autoBuild"] = bool(data.get("autoBuild", True))


def _lol_cfg_save():
    with _lol_cfg_lock:
        _save_json(LOL_FILE, {"autoAccept": _lol_cfg["autoAccept"], "autoBuild": _lol_cfg["autoBuild"]})


def lol_set_auto_accept(on):
    with _lol_cfg_lock:
        _lol_cfg["autoAccept"] = bool(on)
    _lol_cfg_save()
    if on:
        record_feature("lol_auto_accept")
    return {"ok": True, "autoAccept": bool(on)}


def lol_ready_check():
    try:
        status, j = lcu_get("/lol-matchmaking/v1/ready-check")
    except LcuError:
        status, j = 0, None
    with _lol_cfg_lock:
        auto = _lol_cfg["autoAccept"]
    if status != 200 or not j:
        return {"ok": True, "state": "Invalid", "autoAccept": auto}
    return {"ok": True, "state": j.get("state"),
            "playerResponse": j.get("playerResponse"),
            "timer": j.get("timer"), "autoAccept": auto}


def lol_accept_ready_check():
    try:
        status, _ = lcu_post("/lol-matchmaking/v1/ready-check/accept")
    except LcuError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": status in (200, 204)}


LOL_WATCH_INTERVAL = 1.0
# Annehmen/Bannen/Picken laufen seit 2.5.0-dev17 in einem eigenen, schnellen Takt:
# Vorher teilten sie sich die Sekunden-Schleife mit den Auto-Builds, und deren
# Abfrage an rankyoinker.de (Matchup-Daten) hielt die ganze Schleife teils
# Sekunden auf - "Auto-Pick dauert lange" (Nutzer). Gefragt wird nur die Phase
# (ein kurzer LCU-Aufruf), Ready-Check und Champ-Select erst, wenn sie laufen.
LOL_FAST_INTERVAL = 0.35


def _lol_fast_watcher():
    while True:
        time.sleep(LOL_FAST_INTERVAL)
        try:
            if not league_present():
                time.sleep(1.0)
                continue
            status, phase = lcu_get("/lol-gameflow/v1/gameflow-phase")
            phase = phase if status == 200 and isinstance(phase, str) else ""
        except Exception:
            time.sleep(1.0)
            continue
        if phase == "ReadyCheck":
            try:
                with _lol_cfg_lock:
                    auto = _lol_cfg["autoAccept"]
                if auto:
                    status, rc = lcu_get("/lol-matchmaking/v1/ready-check")
                    if status == 200 and isinstance(rc, dict):
                        if rc.get("state") == "InProgress" and rc.get("playerResponse") == "None":
                            lcu_post("/lol-matchmaking/v1/ready-check/accept")
            except Exception:
                pass
        if phase == "ChampSelect" or _lol_auto.get("phase") is not None:
            try:
                _lol_auto_tick()
            except Exception as e:
                # Bewusst mitschreiben statt nur zu verschlucken: ein Absturz hier
                # sah bisher von aussen exakt so aus wie "tut einfach nichts" —
                # ohne Spur war das nur noch Raten.
                _lol_auto["lastCrash"] = {"error": "%s: %s" % (type(e).__name__, e), "at": time.time()}
                report_error("lol-auto", e)
        elif phase not in ("ReadyCheck", "Matchmaking"):
            time.sleep(0.65)          # Lobby/Spiel/kein Client: gemaechlicher fragen


def _lol_watcher():
    while True:
        time.sleep(LOL_WATCH_INTERVAL)
        try:
            if not league_present():
                continue
        except Exception:
            continue
        try:
            gs = lol_gamestate()
            _lol_report_track(gs.get("phase"))
            _lol_report_tick(time.time())
        except Exception as e:
            report_error("lol-match-report", e)
        try:
            _lol_autobuild_tick()
        except Exception as e:
            report_error("lol-autobuild", e)
        try:
            if lol_gamestate().get("state") == "INGAME":
                _lol_record_encounter_now()
        except Exception:
            pass


# ---- Champion-Auswahl (nur Anzeige, kein Auto-Pick) ----

def _lol_resolve_names(summoner_ids):
    """summonerId -> {puuid, name, tagLine} — die LCU kennt keinen Sammel-
    Endpunkt dafuer wie VALORANT ihn hat, darum eine Anfrage pro Spieler."""
    out = {}
    for sid in {s for s in summoner_ids if s}:
        try:
            status, j = lcu_get("/lol-summoner/v1/summoners/%s" % sid)
        except LcuError:
            continue
        if status == 200 and j:
            out[sid] = {"puuid": j.get("puuid"),
                        "name": j.get("gameName") or j.get("displayName"),
                        "tagLine": j.get("tagLine")}
    return out


def _lol_cs_summoners(j, cells):
    """{cellId: Platz-Objekt aus /lol-champ-select/v1/summoners/{slotId}} für
    die gewünschten cellIds.

    Der Pfad nimmt die PLATZNUMMER aus Sicht des Clients (0-4 = eigenes Team,
    danach die Gegner), nicht die cellId. Bis 2.4.7 wurde hier die cellId
    eingesetzt: Auf der roten Seite haben die Gegner die cellIds 0-4, also
    standen bei ihnen die Champions der eigenen Mitspieler - auch wenn sie noch
    gar nichts gewählt hatten (Nutzer-Meldung 2026-10-14). Das Objekt nennt
    seine cellId selbst; nur wenn sie fehlt, gilt die Reihenfolge myTeam, theirTeam."""
    if not cells:
        return {}
    mine = [c.get("cellId") for c in (j.get("myTeam") or [])]
    order = mine + [c.get("cellId") for c in (j.get("theirTeam") or [])]
    want = set(cells)
    found = {}

    def ask(slot):
        try:
            st, sm = lcu_get("/lol-champ-select/v1/summoners/%d" % slot)
        except LcuError:
            return None
        if st != 200 or not isinstance(sm, dict):
            return None
        cell = sm.get("cellId")
        if not isinstance(cell, int) or isinstance(cell, bool):
            cell = order[slot] if slot < len(order) else None
        team = sm.get("isOnPlayersTeam")
        if cell is None or (isinstance(team, bool) and team != (cell in mine)):
            return None                       # passt nicht zusammen - lieber nichts
        return cell, sm

    # Erst die erwarteten Plätze; stimmt die Reihenfolge nicht, alle abfragen
    guessed = sorted(order.index(c) for c in want if c in order)
    for slot in guessed:
        r = ask(slot)
        if r and r[0] in want:
            found[r[0]] = r[1]
        if r and r[0] != order[slot]:
            break
    else:
        return found
    for slot in range(len(order)):
        r = ask(slot)
        if r and r[0] in want:
            found[r[0]] = r[1]
    return found


def _lol_cs_champions(j):
    """{cellId: (championId, nur_gehovert)} für alle Plätze im Champ Select.

    Riots neuere Champ-Select-Version füllt myTeam/theirTeam[].championId nicht
    in jedem Modus zuverlässig (Nutzer-Meldung 2026-10-12: weder eigene noch
    gegnerische Picks sichtbar). Darum mehrere Quellen, erste mit Wert gewinnt:
      1. championId des Platzes,
      2. die Pick-Aktionen (gelockt bzw. gerade gehovert),
      3. /lol-champ-select/v1/summoners/{slotId} - daraus liest die
         Client-Oberfläche selbst (nur für Plätze ohne Treffer, kostet je
         Platz eine LCU-Anfrage; Platz != cellId, siehe _lol_cs_summoners),
      4. championPickIntent (Hover in Planungsphase/fremden Zügen)."""
    out = {}
    cells = (j.get("myTeam") or []) + (j.get("theirTeam") or [])
    picks = {}
    for group in (j.get("actions") or []):
        for a in (group if isinstance(group, list) else [group]):
            if not isinstance(a, dict) or a.get("type") != "pick" or not a.get("championId"):
                continue
            prev = picks.get(a.get("actorCellId"))
            # Gelockte Picks schlagen reine Hover; sonst gewinnt die spätere Aktion
            if not prev or a.get("completed") or prev[1]:
                picks[a.get("actorCellId")] = (a["championId"], not a.get("completed"))
    for c in cells:
        cid = c.get("cellId")
        if c.get("championId"):
            out[cid] = (c["championId"], bool(picks.get(cid) and picks[cid][1]))
        elif cid in picks:
            out[cid] = picks[cid]
    missing = [c for c in cells if c.get("cellId") is not None and c.get("cellId") not in out]
    for cell, sm in _lol_cs_summoners(j, [c.get("cellId") for c in missing]).items():
        if sm.get("championId"):
            out[cell] = (sm["championId"], not sm.get("isDonePicking", True) or bool(sm.get("isPickIntenting")))
    for c in missing:
        if c.get("cellId") not in out and c.get("championPickIntent"):
            out[c.get("cellId")] = (c["championPickIntent"], True)
    # Nichts gefunden, obwohl die Wahl schon durch ist -> Struktur (ohne
    # persönliche Daten) melden, damit der nächste Fall nachvollziehbar ist.
    phase = (j.get("timer") or {}).get("phase")
    if phase == "FINALIZATION" and cells and not out:
        keys = sorted((cells[0] or {}).keys())
        report_error("lol-champselect", "keine Champion-IDs: legacy=%s queue=%s actions=%s cellKeys=%s" % (
            j.get("isLegacyChampSelect"), j.get("queueId"), len(j.get("actions") or []), ",".join(keys)[:300]))
    return out


def lol_champ_select():
    try:
        status, j = lcu_get("/lol-champ-select/v1/session")
    except LcuError as e:
        return {"ok": False, "error": str(e)}
    if status != 200 or not j:
        return {"ok": False, "error": "Gerade keine Champion-Auswahl aktiv."}

    my_ids = [c.get("summonerId") for c in (j.get("myTeam") or []) if c.get("summonerId")]
    names = _lol_resolve_names(my_ids) if my_ids else {}
    # Neuere Champ-Select-Version: Name und puuid stehen direkt am Platz, die
    # summonerId kann 0 sein - dann diese Felder nehmen.
    for c in (j.get("myTeam") or []):
        sid = c.get("summonerId") or ("cell", c.get("cellId"))
        if not (names.get(sid) or {}).get("puuid") and (c.get("puuid") or c.get("gameName")):
            names[sid] = {"puuid": c.get("puuid") or None, "name": c.get("gameName") or None,
                          "tagLine": c.get("tagLine") or None}
    local_cell = j.get("localPlayerCellId")

    puuids = [info.get("puuid") for info in names.values() if info.get("puuid")]
    ranks = dict(zip(puuids, _pool.map(lol_ranked, puuids))) if puuids else {}
    champs = _lol_cs_champions(j)

    def cell(c, with_name):
        info = (names.get(c.get("summonerId") or ("cell", c.get("cellId"))) or {}) if with_name else {}
        r = ranks.get(info.get("puuid")) or {}
        return {
            "cellId": c.get("cellId"), "puuid": info.get("puuid"),
            "name": info.get("name") or ("—" if with_name else None),
            "tagLine": info.get("tagLine"),
            "championId": (champs.get(c.get("cellId")) or (None, False))[0],
            # True = nur gehovert/beabsichtigt, noch nicht gelockt
            "hovering": (champs.get(c.get("cellId")) or (None, False))[1],
            "pickIntent": c.get("championPickIntent") or None,
            # Grossschreiben: normale/Ranked-Queues liefern "TOP", Custom-Spiele
            # können es klein liefern ("top") — sonst griff weder die
            # Preset-Auswahl (Schlüssel sind gross) noch die Lane-Anzeige.
            "position": (c.get("assignedPosition") or "").upper() or None,
            "me": c.get("cellId") == local_cell,
            "rankSolo": r.get("RANKED_SOLO_5x5"), "rankFlex": r.get("RANKED_FLEX_SR"),
        }

    my_team = [cell(c, True) for c in (j.get("myTeam") or [])]
    # Das gegnerische Team ist nur in manchen Modi (z. B. ARAM) sichtbar —
    # dann fehlt summonerId und es bleibt beim Champion.
    their_team = [cell(c, False) for c in (j.get("theirTeam") or [])]

    # Die EIGENE, gerade laufende Aktion (Pick oder Ban) — nur dafür darf
    # überhaupt geklickt werden, alle anderen Aktionen sind fremde Spieler.
    my_action = None
    banned_ids = set()
    ally_cells = {c.get("cellId") for c in (j.get("myTeam") or [])}
    bans = {"ally": [], "enemy": []}
    for group in (j.get("actions") or []):
        for a in group:
            if (a.get("actorCellId") == local_cell and a.get("isInProgress")
                    and a.get("type") in ("pick", "ban")):
                my_action = {"id": a.get("id"), "type": a.get("type"),
                            "championId": a.get("championId") or None,
                            "completed": bool(a.get("completed"))}
            if a.get("type") == "ban":
                slot = _lol_ban_slot(a)
                if slot["state"] == "done":
                    banned_ids.add(slot["championId"])
                ally = a.get("isAllyAction")
                if not isinstance(ally, bool):
                    ally = a.get("actorCellId") in ally_cells
                bans["ally" if ally else "enemy"].append(slot)
    if not bans["ally"] and not bans["enemy"]:
        # Ältere/andere Modi: nur die fertigen Listen, keine Bann-Aktionen
        old = j.get("bans") or {}
        for side, key in (("ally", "myTeamBans"), ("enemy", "theirTeamBans")):
            for cid in (old.get(key) or []):
                slot = _lol_ban_slot({"championId": cid, "completed": True})
                if slot["state"] == "done":
                    banned_ids.add(slot["championId"])
                bans[side].append(slot)

    pickable, bannable = [], []
    try:
        st_p, jp = lcu_get("/lol-champ-select/v1/pickable-champion-ids")
        if st_p == 200 and isinstance(jp, list):
            pickable = jp
    except LcuError:
        pass
    try:
        st_b, jb = lcu_get("/lol-champ-select/v1/bannable-champion-ids")
        if st_b == 200 and isinstance(jb, list):
            bannable = jb
    except LcuError:
        pass

    # Mastery gibt es nur für die eigene Auswahl — die LCU verrieie die
    # Mastery-Werte anderer Spieler grundsätzlich nicht.
    me_cell = next((m for m in my_team if m["me"]), None)
    my_mastery = lol_champion_mastery(me_cell["championId"]) if me_cell else None

    timer = j.get("timer") or {}
    total = timer.get("totalTimeInPhase")
    return {"ok": True, "myTeam": my_team, "theirTeam": their_team,
            "myAction": my_action, "pickable": pickable, "bannable": bannable,
            "bannedChampionIds": sorted(banned_ids), "bans": bans,
            "myMastery": my_mastery,
            # Restzeit erst hier ausrechnen: Ränge und Mastery oben kosten Zeit
            "phase": timer.get("phase"), "timeLeft": _lol_cs_time_left(timer),
            # Länge des laufenden Zugs für den Ring (unplausibel lang -> weglassen)
            "timeTotal": total if _is_num(total) and 0 < total <= 180000 and not timer.get("isInfinite") else None}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _lol_ban_slot(a):
    """Ein Bann-Platz für die Anzeige. Ein abgeschlossener Bann ohne Champion
    (die LCU liefert dann -1 oder 0) heisst "nicht gebannt" - die -1 landete
    früher als Champion-ID in der Liste."""
    cid = a.get("championId")
    cid = cid if isinstance(cid, int) and not isinstance(cid, bool) and cid > 0 else None
    if a.get("completed"):
        return {"championId": cid, "state": "done" if cid else "none"}
    if a.get("isInProgress"):
        return {"championId": cid, "state": "active"}     # cid = Hover (beim Gegner verborgen)
    return {"championId": None, "state": "pending"}


_lol_cs_timer_seen = {"key": None, "at": 0.0}


def _lol_cs_time_left(timer):
    """Restzeit (ms) der laufenden Champ-Select-Phase, Stand JETZT.

    Die LCU schreibt "timer" nur neu, wenn sich die Session ändert (Hovern,
    Lock, neuer Zug). "adjustedTimeLeftInPhase" ist die Restzeit zum Zeitpunkt
    "internalNowInEpochMs", nicht zum Zeitpunkt der Anfrage - ohne Abzug sprang
    die Uhr im Overlay bei jedem Poll auf diesen alten Wert zurück, bis jemand
    etwas hoverte."""
    left = timer.get("adjustedTimeLeftInPhase")
    if timer.get("isInfinite") or not _is_num(left):
        return None
    snap = timer.get("internalNowInEpochMs")
    elapsed = None
    if _is_num(snap) and snap > 0:
        e = time.time() * 1000 - snap
        # Der League-Client läuft auf demselben PC, also dieselbe Uhr. Ein
        # völlig unplausibler Abstand heisst: andere Uhr - dann unten zählen.
        if -2000 <= e <= 15 * 60 * 1000:
            elapsed = max(0.0, e)
    if elapsed is None:
        key = (timer.get("phase"), snap, left)
        if _lol_cs_timer_seen["key"] != key:
            _lol_cs_timer_seen.update(key=key, at=time.monotonic())
        elapsed = (time.monotonic() - _lol_cs_timer_seen["at"]) * 1000
    return max(0, int(left - elapsed))


def lol_champ_action(action_id, champion_id, lock):
    """Hovern (lock=False) oder fest waehlen/bannen (lock=True) — ausschliesslich
    fuer die EIGENE, gerade laufende Aktion (actionId kommt von lol_champ_select).

    Bewusst "is None" statt "not action_id": die allererste Aktion einer
    Champ-Select-Sitzung hat oft die ID 0 (z. B. in kleinen Custom-Lobbys) —
    "not 0" ist in Python True, das hielt eine ganz normale Aktion faelschlich
    fuer "fehlt" und brach ab, ohne die LCU ueberhaupt zu fragen."""
    if action_id is None or not champion_id:
        return {"ok": False, "error": "Kein Champion ausgewaehlt."}
    champ_id = int(champion_id)
    path = "/lol-champ-select/v1/session/actions/%s" % action_id
    try:
        if lock:
            # Der übliche, direkte Weg: EIN PATCH mit championId UND
            # completed:true sperrt sofort, ohne Zeitfenster zwischen Hovern
            # und Sperren, in dem ein zweiter, separater Aufruf abgelehnt
            # werden könnte (das war der Bug: hoverte zuverlässig, sperrte
            # aber nie — der Spieler stand die ganze Auswahlzeit nur gehovert).
            status, _ = lcu_patch(path, {"championId": champ_id, "completed": True})
            if status in (200, 204):
                return {"ok": True, "status": status}
            # Ältere LCU-Versionen wollen stattdessen zwei getrennte Aufrufe:
            # erst hovern, dann über einen eigenen /complete-Aufruf sperren.
            status, _ = lcu_patch(path, {"championId": champ_id})
            if status not in (200, 204):
                return {"ok": False, "status": status, "error": "Riot lehnte ab (Status %s)." % status}
            status, _ = lcu_post(path + "/complete", {"championId": champ_id})
            if status not in (200, 204):
                return {"ok": False, "status": status, "error": "Sperren fehlgeschlagen (Status %s)." % status}
        else:
            status, _ = lcu_patch(path, {"championId": champ_id})
            if status not in (200, 204):
                return {"ok": False, "status": status, "error": "Riot lehnte ab (Status %s)." % status}
    except LcuError as e:
        return {"ok": False, "status": None, "error": str(e)}
    return {"ok": True, "status": status}


# ---- Presets: automatisches Bannen und Wählen nach Lane ----
# "Preset Ban" und "Preset Pick" - je ein Champion (plus Ausweich) je Lane,
# erkannt über assignedPosition - einmal eingestellt und dauerhaft
# gespeichert, wie "Automatisch annehmen" oben. Ausgelöst wird über denselben
# Wächter-Takt (_lol_watcher), damit auch hier ein Hintergrund-Tab nichts
# verpasst.

LOL_PRESETS_FILE = os.path.join(DATA_DIR, "vry_lol_presets.json")
LOL_LANES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
# Modi ohne Lane (Arena, ARAM, ...) liefern kein assignedPosition — dafür gibt
# es diesen zusätzlichen Slot als Ausweichlösung, siehe _lol_auto_tick().
LOL_PICK_SLOTS = LOL_LANES + ("OTHER",)
# Je Lane bis zu drei Champions in Priorität: Hauptpick + zwei Ausweichchampions,
# falls der Hauptpick gebannt oder von jemand anderem schon gewählt ist.
LOL_PICK_PRIORITIES = 3
_lol_presets_lock = threading.Lock()
_lol_presets = {
    "autoBan": False, "autoPick": False,
    # Je Lane bis zu drei Bann-Ziele in Priorität — genau wie bei Picks: Wird
    # der Hauptbann vorher schon vom Gegner-/Ally-Team gebannt (häufig bei
    # starken Champions), springt Auto-Bann sonst tatenlos ab. Siehe
    # _lol_auto_tick(). Gleiche Struktur wie "picks" (dieselbe Lane-basierte
    # Auswahl ergibt bei Bann genauso viel Sinn wie beim Pick, z.B. "gegen
    # Top will ich XY bannen, gegen Jungle etwas anderes").
    "ban": {},         # {"TOP": [{"id":..,"name":..}|None, ...bis zu 3 Eintraege], ...}
    "picks": {},       # {"TOP": [{"id":..,"name":..}|None, ...bis zu 3 Eintraege], ...}
}

# Kleine, zufällige Verzögerung vor dem Sperren, damit ein Preset nicht
# unmenschlich sofort feuert, sobald die eigene Aktion beginnt. Gehovert wird
# trotzdem sofort (siehe _lol_auto_tick) — nur das Sperren wartet. Bis dev16
# 2,0 s +/- 1,2 s ("Pick dauert lange"), seit dev17 0,4 bis 1 s.
LOL_AUTO_DELAY = 0.7
LOL_AUTO_JITTER = 0.3
# Ein Versuch je Takt des schnellen Watchers (~3x/Sekunde) statt einer Serie —
# ein einzelner abgelehnter Sperrversuch (z. B. weil die LCU kurz nach dem
# Hovern noch nicht so weit ist) darf nicht dauerhaft aufgeben. Erst nach
# LOL_AUTO_MAX_ATTEMPTS Versuchen ohne Erfolg wird's dem manuellen Klicken
# überlassen. Gezaehlt wird nur ausserhalb der Planungsphase - siehe
# _lol_auto_tick(): dort ist Sperren grundsaetzlich nicht moeglich, und
# frueher verbrannte eine lange Planungsphase das ganze Budget, bevor
# Sperren ueberhaupt erlaubt war.
LOL_AUTO_MAX_ATTEMPTS = 40       # ~14 s bei LOL_FAST_INTERVAL
LOL_AUTO_HISTORY_MAX = 20

_lol_auto = {"actionId": None, "hoveredChampionId": None, "fireAt": 0.0, "attempts": 0,
             "lastResult": None, "lastSkip": None,
             # Vorab-Pick (Absicht) auf der eigenen, noch NICHT laufenden Pick-Aktion
             "intentKey": None, "intentFailed": set(),
             "phase": None, "alert": None}
_lol_auto_history = collections.deque(maxlen=LOL_AUTO_HISTORY_MAX)
_lol_auto_history_lock = threading.Lock()


def _lol_auto_reset():
    _lol_auto.update({"actionId": None, "hoveredChampionId": None, "fireAt": 0.0, "attempts": 0})


def _lol_auto_session_reset():
    """Champion-Auswahl vorbei/nicht aktiv: auch Absicht, Phase und Hinweis vergessen."""
    _lol_auto_reset()
    _lol_auto.update({"intentKey": None, "intentFailed": set(), "phase": None, "alert": None})


def _lol_auto_log(kind, action_type, action_id, champion_id, result, phase):
    """Jeder Hover/Absicht/Sperrversuch landet hier - lastResult allein zeigte
    nur den jeweils letzten Versuch, fruehere Fehlschlaege waren unsichtbar."""
    entry = {"at": time.time(), "kind": kind, "actionType": action_type, "actionId": action_id,
             "championId": champion_id, "ok": bool(result.get("ok")),
             "status": result.get("status"), "error": result.get("error"), "phase": phase}
    with _lol_auto_history_lock:
        _lol_auto_history.append(entry)


def _lol_priority_slots(entries):
    """Rohe Eintragsliste -> feste Liste mit LOL_PICK_PRIORITIES Plaetzen
    (fehlende/ungueltige Eintraege werden None)."""
    slots = [None] * LOL_PICK_PRIORITIES
    for i, e in enumerate((entries or [])[:LOL_PICK_PRIORITIES]):
        if isinstance(e, dict) and e.get("id"):
            slots[i] = {"id": e["id"], "name": e.get("name", "")}
    return slots


def _lol_priority_dict(raw):
    """Rohe {Lane: Eintragsliste}-Struktur -> bereinigtes Dict mit festen
    LOL_PICK_PRIORITIES-Plaetzen je Lane (wie bei picks). Laesst unbekannte
    Lanes und leere Eintraege weg."""
    src = raw if isinstance(raw, dict) else {}
    cleaned = {}
    for lane, entries in src.items():
        if lane not in LOL_PICK_SLOTS:
            continue
        # Aeltere Speicherstaende hatten nur einen Eintrag je Lane (ein
        # einzelnes {"id":..,"name":..} statt einer Prioritaetsliste) — der
        # wird als Slot 0 uebernommen, statt beim Laden zu verschwinden.
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        slots = _lol_priority_slots(entries)
        if any(slots):
            cleaned[lane] = slots
    return cleaned


def _lol_presets_restore():
    data = _load_json(LOL_PRESETS_FILE)
    if not isinstance(data, dict):
        return
    with _lol_presets_lock:
        _lol_presets["autoBan"] = bool(data.get("autoBan"))
        _lol_presets["autoPick"] = bool(data.get("autoPick"))
        ban = data.get("ban")
        if isinstance(ban, list):
            # Noch aelterer Speicherstand: EIN lane-uebergreifender Bann
            # (Prioritaetsliste ohne Lane-Aufteilung) statt je Lane. Wird auf
            # jede Lane uebernommen, statt beim Umstieg auf Per-Lane-Baenne
            # zu verschwinden - der Nutzer kann die Lanes danach frei
            # auseinanderziehen.
            slots = _lol_priority_slots(ban)
            _lol_presets["ban"] = {lane: list(slots) for lane in LOL_LANES} if any(slots) else {}
        else:
            _lol_presets["ban"] = _lol_priority_dict(ban)
        _lol_presets["picks"] = _lol_priority_dict(data.get("picks"))


def _lol_presets_save():
    with _lol_presets_lock:
        _save_json(LOL_PRESETS_FILE, dict(_lol_presets))


def lol_presets_state():
    with _lol_auto_history_lock:
        history = list(_lol_auto_history)
    with _lol_presets_lock:
        return {"ok": True, "autoBan": _lol_presets["autoBan"], "autoPick": _lol_presets["autoPick"],
                "ban": _lol_presets["ban"], "picks": _lol_presets["picks"],
                "lastAutoHover": _lol_auto.get("lastHover"),
                "lastAutoResult": _lol_auto.get("lastResult"),
                "lastAutoSkip": _lol_auto.get("lastSkip"),
                "lastAutoCrash": _lol_auto.get("lastCrash"),
                "autoHistory": history,
                "autoAlert": _lol_auto.get("alert")}


def lol_presets_set_ban(position, slot, champion_id, name):
    if position not in LOL_PICK_SLOTS:
        return {"ok": False, "error": "Unbekannte Lane."}
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        slot = 0
    if slot not in range(LOL_PICK_PRIORITIES):
        return {"ok": False, "error": "Unbekannter Prio-Platz."}
    with _lol_presets_lock:
        entries = _lol_presets["ban"].setdefault(position, [None] * LOL_PICK_PRIORITIES)
        if champion_id:
            entries[slot] = {"id": int(champion_id), "name": name or ""}
        else:
            entries[slot] = None
        if not any(entries):
            _lol_presets["ban"].pop(position, None)
    _lol_presets_save()
    return lol_presets_state()


def lol_presets_set_pick(position, slot, champion_id, name):
    if position not in LOL_PICK_SLOTS:
        return {"ok": False, "error": "Unbekannte Lane."}
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        slot = 0
    if slot not in range(LOL_PICK_PRIORITIES):
        return {"ok": False, "error": "Unbekannter Prio-Platz."}
    with _lol_presets_lock:
        entries = _lol_presets["picks"].setdefault(position, [None] * LOL_PICK_PRIORITIES)
        if champion_id:
            entries[slot] = {"id": int(champion_id), "name": name or ""}
        else:
            entries[slot] = None
        if not any(entries):
            _lol_presets["picks"].pop(position, None)
    _lol_presets_save()
    return lol_presets_state()


def lol_presets_set_auto(ban=None, pick=None):
    with _lol_presets_lock:
        if ban is not None:
            _lol_presets["autoBan"] = bool(ban)
        if pick is not None:
            _lol_presets["autoPick"] = bool(pick)
    _lol_presets_save()
    if ban:
        record_feature("lol_auto_ban")
    if pick:
        record_feature("lol_auto_pick")
    return lol_presets_state()


def _lol_priority_ids(slots):
    """Champion-IDs einer Prioritaetsliste (Hauptziel zuerst, dann die
    Ausweichchampions) — leere Slots werden uebersprungen."""
    return [s["id"] for s in (slots or []) if s and s.get("id")]


def _lol_auto_lane_candidates(entries_by_lane, lane):
    """Champion-IDs in Prioritaetsreihenfolge fuer eine Lane (Hauptziel zuerst,
    dann die Ausweichchampions) — leere Slots werden uebersprungen. Gilt
    gleichermassen fuer Baenne und Picks, beide sind {Lane: [...]} strukturiert.

    Faellt auf den lanelosen "OTHER"-Eintrag zurueck, wenn entweder die Lane
    noch gar nicht bekannt ist ODER fuer die (bekannte) Lane nichts hinterlegt
    wurde. Der erste Fall ist der Grund fuer den Bug "die ersten paar Sekunden
    von Champ-Select passiert nichts": assignedPosition steht erst fest,
    nachdem die kurze Rollentausch-Phase am Anfang durch ist - bis dahin war
    lane="" und ohne diesen Fallback blieb Auto-Ban/-Pick in der Zwischenzeit
    komplett stumm (kein Hover, kein Fehler, nichts), obwohl in dieser Zeit
    z.B. ein Team-Mitglied genau den eigenen Zielchampion wegbannen konnte."""
    primary = _lol_priority_ids(entries_by_lane.get(lane)) if lane else []
    return primary if primary else _lol_priority_ids(entries_by_lane.get("OTHER"))


def _lol_champ_select_for_auto():
    """Schlanke Variante von lol_champ_select() nur fuer _lol_auto_tick():
    die volle Funktion loest fuer JEDEN Mitspieler Namen auf (eine LCU-Anfrage
    pro Person, nacheinander) und holt zusaetzlich Raenge - Daten, die nur die
    UI-Kartenanzeige braucht, die Automatik selbst aber gar nicht. Dadurch war
    jeder einzelne Watcher-Takt spuerbar langsam (mehrere sequenzielle LCU-
    Requests), was den allerersten Hover nach Champ-Select-Start um mehrere
    Sekunden verzoegern konnte, obwohl die eigene Lane laengst feststand."""
    try:
        status, j = lcu_get("/lol-champ-select/v1/session")
    except LcuError as e:
        return {"ok": False, "error": str(e)}
    if status != 200 or not j:
        return {"ok": False, "error": "Gerade keine Champion-Auswahl aktiv."}

    local_cell = j.get("localPlayerCellId")
    raw_my_team = j.get("myTeam") or []
    me_raw = next((c for c in raw_my_team if c.get("cellId") == local_cell), None)
    mate_champs = {c.get("championId") for c in raw_my_team
                   if c.get("cellId") != local_cell and c.get("championId")}
    enemy_champs = {c.get("championId") for c in (j.get("theirTeam") or []) if c.get("championId")}

    my_action = None
    # Die eigene Pick-Aktion auch dann, wenn sie (noch) NICHT laeuft - in der
    # Planungsphase und waehrend fremder Zuege ist sie nicht isInProgress, und
    # nur ueber sie laesst sich vorab eine Absicht (Pre-Pick) setzen.
    my_pick_action = None
    banned_ids = set()
    locked_picks = set()
    for group in (j.get("actions") or []):
        for a in group:
            mine = a.get("actorCellId") == local_cell
            if mine and a.get("isInProgress") and a.get("type") in ("pick", "ban"):
                my_action = {"id": a.get("id"), "type": a.get("type"), "completed": bool(a.get("completed"))}
            if mine and a.get("type") == "pick" and not a.get("completed") and my_pick_action is None:
                my_pick_action = {"id": a.get("id"), "isInProgress": bool(a.get("isInProgress"))}
            if a.get("type") == "ban" and a.get("completed") and a.get("championId"):
                banned_ids.add(a["championId"])
            if a.get("type") == "pick" and a.get("completed") and a.get("championId"):
                locked_picks.add(a["championId"])

    pickable, bannable = [], []
    try:
        st_p, jp = lcu_get("/lol-champ-select/v1/pickable-champion-ids")
        if st_p == 200 and isinstance(jp, list):
            pickable = jp
    except LcuError:
        pass
    try:
        st_b, jb = lcu_get("/lol-champ-select/v1/bannable-champion-ids")
        if st_b == 200 and isinstance(jb, list):
            bannable = jb
    except LcuError:
        pass

    return {"ok": True, "myAction": my_action, "myPickAction": my_pick_action,
            "phase": (j.get("timer") or {}).get("phase"),
            "pickable": pickable, "bannable": bannable,
            "bannedChampionIds": sorted(banned_ids), "lockedPicks": locked_picks,
            "mateChamps": mate_champs, "enemyChamps": enemy_champs,
            "myPosition": (me_raw.get("assignedPosition") or "").upper() or None if me_raw else None}


def _lol_ban_choice(bans, lane, cs):
    """(Ziel-ID oder None, Grund falls None) fuer die eigene Bann-Aktion."""
    candidates = _lol_auto_lane_candidates(bans, lane)
    bannable = set(cs.get("bannable") or [])
    # Ausserhalb eines beschränkten Champion-Pools (Turnier-Modus o.ä.)
    # liefert die LCU hier nur den Platzhalter [-1] statt einer echten
    # Liste - als Verbotsliste ausgelegt bannte Auto-Bann dann NIE
    # etwas, obwohl ganz normal gebannt werden kann. In dem Fall zählt
    # nur, was in dieser Session schon gebannt wurde.
    if bannable in (set(), {-1}):
        banned_ids = set(cs.get("bannedChampionIds") or [])
        bannable = set(_lol_champion_catalog()["byId"]) - banned_ids
    mate_champs = cs.get("mateChamps") or set()
    desired = next((cid for cid in candidates if cid in bannable and cid not in mate_champs), None)
    if desired is not None:
        return desired, None
    return None, (
        "Keiner der hinterlegten Bann-Champions fuer diese Lane ist gerade bannbar "
        "(schon gebannt oder von einem Team-Mitglied gehovert)." if candidates
        else "Kein Bann-Preset fuer Lane '%s' (und auch keins unter 'Andere Modi') hinterlegt." % (lane or "unbekannt"))


def _lol_pick_choice(picks, lane, cs):
    """(Ziel-ID oder None, Grund falls None, ob ueberhaupt Presets hinterlegt
    sind) - gemeinsam fuer den Vorab-Pick (Absicht) und den eigenen Zug, damit
    beide exakt dieselbe Verfuegbarkeit pruefen."""
    candidates = _lol_auto_lane_candidates(picks, lane)
    banned_ids = set(cs.get("bannedChampionIds") or [])
    locked = set(cs.get("lockedPicks") or set())
    mate_champs = cs.get("mateChamps") or set()
    enemy_champs = cs.get("enemyChamps") or set()
    pickable = set(cs.get("pickable") or [])
    # Dieselbe Riot-Eigenheit wie bei "bannable": ausserhalb eines
    # beschraenkten Champion-Pools liefert die LCU "pickable-champion-ids"
    # nach einem Bann manchmal nur kurz den Platzhalter [-1] oder eine
    # leere Liste statt der echten, aktualisierten Liste. Ohne diesen
    # Fallback war dann PLOETZLICH JEDER Kandidat "nicht pickbar", auch
    # der eigentlich freie 2./3. Ausweich. Fallback zaehlt nur, was WIRKLICH weg ist.
    if pickable in (set(), {-1}):
        pickable = set(_lol_champion_catalog()["byId"]) - banned_ids - mate_champs - enemy_champs
    avail = [cid for cid in candidates
             if cid in pickable and cid not in banned_ids and cid not in locked]
    # Erst den Ausweich bevorzugen, der nicht schon beim Gegner hängt (kein
    # Mirror-Match); hängt der Gegner an ALLEN hinterlegten Kandidaten,
    # lieber den bestplatzierten davon nehmen als am Ende gar nichts zu picken.
    desired = next((cid for cid in avail if cid not in enemy_champs), None)
    if desired is None and avail:
        desired = avail[0]
    if desired is not None:
        return desired, None, True
    return None, (
        "Keiner der hinterlegten Ausweichchampions fuer diese Lane ist gerade waehlbar." if candidates
        else "Kein Pick-Preset fuer Lane '%s' (und auch keins unter 'Andere Modi') hinterlegt." % (lane or "unbekannt")
    ), bool(candidates)


def _lol_auto_set_alert(reason, action_id):
    """Alle hinterlegten Picks sind weg - deutlich im UI melden (inkl. Ton),
    damit der Spieler rechtzeitig selbst waehlt, statt beim Timer-Ablauf aus
    der Lobby zu fliegen. Bleibt stehen, solange der Zustand anhaelt."""
    alert = _lol_auto.get("alert")
    if alert and alert.get("actionId") == action_id and alert.get("reason") == reason:
        return
    _lol_auto["alert"] = {"type": "pick", "reason": reason, "actionId": action_id, "at": time.time()}


def _lol_auto_intent(picks, lane, cs, phase):
    """Vorab-Pick: solange die eigene Pick-Aktion noch NICHT laeuft (Planungs-
    phase, fremde Zuege), das beste verfuegbare Preset als Absicht setzen
    (PATCH ohne completed) - und umstellen, sobald es weggebannt/-gepickt wird.
    Gibt False zurueck, wenn kein hinterlegter Pick mehr verfuegbar ist."""
    pa = cs.get("myPickAction")
    if not pa or pa.get("isInProgress"):
        return True     # eigener Zug laeuft (oder es gibt keinen) - das macht der Rest von _lol_auto_tick
    desired, reason, had_candidates = _lol_pick_choice(picks, lane, cs)
    if desired is None:
        if had_candidates:
            _lol_auto_set_alert(reason, pa["id"])
            return False
        return True
    key = (pa["id"], desired)
    if _lol_auto.get("intentKey") == key or key in _lol_auto["intentFailed"]:
        return True
    result = lol_champ_action(pa["id"], desired, False)
    _lol_auto_log("intent", "pick", pa["id"], desired, result, phase)
    if result.get("ok"):
        _lol_auto["intentKey"] = key
    else:
        # Nicht jede Sekunde erneut gegen dieselbe Ablehnung anrennen - ein
        # neues Ziel (z. B. nach einem Bann) wird trotzdem wieder versucht.
        # Ungetestet ist, ob die LCU die Absicht auch waehrend BAN_PICK auf
        # einer nicht laufenden Aktion annimmt; dann steht hier der Status.
        _lol_auto["intentFailed"].add(key)
    return True


def _lol_auto_tick():
    """Einmal je Watcher-Takt.

    1. Vorab-Pick: Absicht auf der eigenen, noch nicht laufenden Pick-Aktion
       setzen (siehe _lol_auto_intent).
    2. Laufende eigene Aktion (Bann oder Pick): sofort hovern; gesperrt wird
       erst nach kurzer, menschlich wirkender Verzoegerung und NUR ausserhalb
       der Planungsphase. In PLANNING kann die LCU nicht sperren - frueher
       wurde dort trotzdem jede Sekunde ein Sperrversuch gemacht, das
       verbrauchte LOL_AUTO_MAX_ATTEMPTS, bevor Sperren ueberhaupt ging
       (dann wurde nie gebannt/gepickt). Beim Wechsel aus PLANNING beginnt
       Verzoegerung und Zaehler neu.
    Die Wahl wird bei jedem Takt neu geprueft: ein Bann bricht ab, sobald ein
    Team-Mitglied den Ziel-Champion hovert, ein Pick wechselt auf den
    naechsten Ausweich, falls der Hauptpick inzwischen weg ist."""
    with _lol_presets_lock:
        auto_ban, auto_pick = _lol_presets["autoBan"], _lol_presets["autoPick"]
        bans = {k: list(v) for k, v in _lol_presets["ban"].items()}
        picks = {k: list(v) for k, v in _lol_presets["picks"].items()}
    if not auto_ban and not auto_pick:
        _lol_auto_session_reset()
        return

    cs = _lol_champ_select_for_auto()
    if not cs.get("ok"):
        _lol_auto_session_reset()
        return

    phase = cs.get("phase")
    prev_phase = _lol_auto.get("phase")
    _lol_auto["phase"] = phase
    if prev_phase == "PLANNING" and phase != "PLANNING" and _lol_auto["actionId"] is not None:
        wait = max(0.0, LOL_AUTO_DELAY + random.uniform(-LOL_AUTO_JITTER, LOL_AUTO_JITTER))
        _lol_auto.update({"fireAt": time.time() + wait, "attempts": 0})

    lane = cs.get("myPosition")
    picks_available = _lol_auto_intent(picks, lane, cs, phase) if auto_pick else True

    action = cs.get("myAction")
    if not action or action.get("completed"):
        _lol_auto_reset()
        if picks_available:
            _lol_auto["alert"] = None
        return

    desired, skip_reason = None, None
    if action.get("type") == "ban":
        if auto_ban:
            desired, skip_reason = _lol_ban_choice(bans, lane, cs)
    elif action.get("type") == "pick" and auto_pick:
        desired, skip_reason, had_candidates = _lol_pick_choice(picks, lane, cs)
        if desired is None and had_candidates:
            _lol_auto_set_alert(skip_reason, action.get("id"))
            picks_available = False
    if picks_available:
        _lol_auto["alert"] = None

    if skip_reason and (_lol_auto.get("lastSkip") or {}).get("actionId") != action.get("id"):
        _lol_auto["lastSkip"] = {"actionId": action.get("id"), "type": action.get("type"),
                                  "reason": skip_reason, "at": time.time()}

    if desired is None:
        _lol_auto_reset()
        return

    # Neue Aktion oder ein anderes Ziel als zuletzt (z. B. Hauptpick weg,
    # jetzt Ausweich 1 dran) -> sofort hovern, Sperr-Timer neu setzen.
    if _lol_auto["actionId"] != action.get("id") or _lol_auto["hoveredChampionId"] != desired:
        hover_result = lol_champ_action(action.get("id"), desired, False)
        _lol_auto["lastHover"] = {"ok": bool(hover_result.get("ok")), "error": hover_result.get("error"),
                                   "championId": desired, "actionId": action.get("id"), "at": time.time()}
        _lol_auto_log("hover", action.get("type"), action.get("id"), desired, hover_result, phase)
        wait = max(0.0, LOL_AUTO_DELAY + random.uniform(-LOL_AUTO_JITTER, LOL_AUTO_JITTER))
        _lol_auto.update({"actionId": action.get("id"), "hoveredChampionId": desired,
                          "fireAt": time.time() + wait, "attempts": 0})
        return
    if phase == "PLANNING":
        return
    if time.time() < _lol_auto["fireAt"] or _lol_auto["attempts"] >= LOL_AUTO_MAX_ATTEMPTS:
        return
    _lol_auto["attempts"] += 1
    result = lol_champ_action(action.get("id"), desired, True)
    _lol_auto_log("lock", action.get("type"), action.get("id"), desired, result, phase)
    _lol_auto["lastResult"] = {"ok": bool(result.get("ok")), "error": result.get("error"),
                                "status": result.get("status"), "phase": phase,
                                "at": time.time(), "championId": desired, "attempts": _lol_auto["attempts"]}


# ---- Builds anwenden: Runen, Beschwörerzauber und Item-Set ----
# Kommt aus den Matchup-Builds im Overlay (Klick auf "Übernehmen"). Die früheren
# gespeicherten Champion-Setups samt automatischem Anwenden sind seit 2.4.2-dev7
# entfernt (Nutzerwunsch); eine alte vry_lol_setups.json wird nicht mehr gelesen.
# Item-Sets erscheinen im Shop im Spiel, ganz ohne Overlay.

LOL_SETUP_PAGE_PREFIX = "RankYoinker"


def _pos_int(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _clean_setup(raw):
    """Prüft ein Setup und gibt die bereinigte Fassung zurück (ValueError mit
    verständlicher Meldung, wenn etwas nicht passt)."""
    if not isinstance(raw, dict):
        raise ValueError("Ungültiges Setup.")
    spells = [s for s in (_pos_int(x) for x in (raw.get("spells") or [])[:2]) if s]
    if len(spells) == 2 and spells[0] == spells[1]:
        raise ValueError("Zweimal derselbe Beschwörerzauber.")
    runes = None
    r = raw.get("runes")
    if isinstance(r, dict):
        perks = [_pos_int(x) for x in (r.get("selectedPerkIds") or [])]
        ps, ss = _pos_int(r.get("primaryStyleId")), _pos_int(r.get("subStyleId"))
        if len(perks) != 9 or not all(perks) or not ps or not ss or ps == ss:
            raise ValueError("Runenseite unvollständig (9 Runen aus zwei verschiedenen Bäumen).")
        runes = {"primaryStyleId": ps, "subStyleId": ss, "selectedPerkIds": perks}
    blocks = []
    for b in (raw.get("items") or [])[:6]:
        if not isinstance(b, dict):
            continue
        ids = [i for i in (_pos_int(x) for x in (b.get("items") or [])[:12]) if i]
        if ids:
            blocks.append({"type": str(b.get("type") or "")[:40], "items": ids})
    return {"name": str(raw.get("name") or "")[:40],
            "spells": spells if len(spells) == 2 else [],
            "runes": runes, "items": blocks}


def lol_apply_build(champion_id, raw, manual=True):
    """Wendet einen Build sofort an - Runen, Item-Set und (nur im Champ Select)
    Beschwörerzauber. Kommt aus den Matchup-Builds im Overlay: der Spieler
    wählt dort selbst einen von mehreren Vorschlägen und klickt auf
    "Übernehmen", es wird nichts automatisch erzwungen."""
    cid = _pos_int(champion_id)
    if not cid:
        return {"ok": False, "error": "Kein Champion gewählt."}
    try:
        setup = _clean_setup(raw)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    name = setup.get("name") or str(cid)
    session = None
    try:
        st, j = lcu_get("/lol-champ-select/v1/session")
        if st == 200 and isinstance(j, dict):
            session = j
    except LcuError as e:
        return {"ok": False, "error": str(e)}
    spells = list(setup["spells"])
    if session and len(spells) == 2:
        # Flash & Co. bleiben auf der Taste, auf der der Spieler sie gerade hat
        local = session.get("localPlayerCellId")
        me = next((c for c in (session.get("myTeam") or []) if c.get("cellId") == local), {})
        cur = (me.get("spell1Id"), me.get("spell2Id"))
        if spells[1] == cur[0] or spells[0] == cur[1]:
            spells.reverse()
    results = {}
    for what, fn in (("runes", lambda: _lol_apply_runes(name, setup["runes"]) if setup["runes"] else None),
                     ("spells", lambda: _lol_apply_spells(spells) if (spells and session) else None),
                     ("items", lambda: _lol_apply_itemset(cid, name, setup["items"]) if setup["items"] else None)):
        try:
            res = fn()
        except LcuError as e:
            res = {"ok": False, "status": None, "code": "client", "error": str(e)}
        if res is None:
            continue
        results[what] = res
        if not res.get("ok"):
            report_error("lol-build", "%s: HTTP %s %s" % (what, res.get("status"), res.get("error") or ""))
    if not results:
        return {"ok": False, "error": "Nichts zum Übernehmen."}
    if manual:
        # Selbst gewählt -> der Auto-Build überschreibt das in diesem Spiel nicht
        # mehr (im Champ Select wie im laufenden Spiel)
        game = session.get("gameId") if session else ((_lol_gameflow() or {}).get("gameData") or {}).get("gameId")
        if game:
            _lol_autobuild["manualGame"] = game
    record_feature("lol_build_applied" if manual else "lol_build_auto")
    return {"ok": all(r.get("ok") for r in results.values()), "results": results}


def _lol_apply_runes(champ_name, runes):
    page = dict(runes, name=("%s: %s" % (LOL_SETUP_PAGE_PREFIX, champ_name))[:30], current=True)
    st, pages = lcu_get("/lol-perks/v1/pages")
    if st != 200 or not isinstance(pages, list):
        return {"ok": False, "status": st, "code": "runes_unreadable", "error": "Runenseiten nicht lesbar."}
    # Ausschließlich die eigene Seite anfassen - Seiten des Spielers bleiben tabu.
    own = next((p for p in pages if p.get("isEditable")
                and str(p.get("name") or "").startswith(LOL_SETUP_PAGE_PREFIX)), None)
    page_id = None
    if own:
        st, _ = lcu_put("/lol-perks/v1/pages/%s" % own.get("id"), page)
        if st in (200, 201, 204):
            page_id = own.get("id")
        else:
            lcu_delete("/lol-perks/v1/pages/%s" % own.get("id"))
    if page_id is None:
        st, created = lcu_post("/lol-perks/v1/pages", page)
        if st not in (200, 201, 204):
            return {"ok": False, "status": st, "code": "no_rune_page",
                    "error": "Keine freie Runenseite - im Client eine löschen oder eine Seite „RankYoinker“ nennen."}
        page_id = (created or {}).get("id") if isinstance(created, dict) else None
    if page_id is not None:
        lcu_put("/lol-perks/v1/currentpage", page_id)
    return {"ok": True, "status": st}


def _lol_apply_spells(spells):
    st, _ = lcu_patch("/lol-champ-select/v1/session/my-selection", {"spell1Id": spells[0], "spell2Id": spells[1]})
    return {"ok": st in (200, 204), "status": st, "code": None if st in (200, 204) else "rejected",
            "error": None if st in (200, 204) else "Riot lehnte ab (Status %s)." % st}


def _lol_apply_itemset(champ_id, champ_name, blocks):
    summoner = lol_current_summoner()
    path = "/lol-item-sets/v1/item-sets/%s/sets" % summoner.get("summonerId")
    st, data = lcu_get(path)
    if st != 200 or not isinstance(data, dict):
        return {"ok": False, "status": st, "code": "itemsets_unreadable", "error": "Item-Sets nicht lesbar."}
    uid = "rankyoinker-%d" % champ_id
    sets = [s for s in (data.get("itemSets") or []) if s.get("uid") != uid]
    sets.append({
        "uid": uid, "title": ("RankYoinker: %s" % champ_name)[:40], "type": "custom",
        "map": "any", "mode": "any", "sortrank": 0, "startedFrom": "blank",
        "associatedChampions": [champ_id], "associatedMaps": [11, 12], "preferredItemSlots": [],
        "blocks": [{"type": b["type"] or "Items", "items": [{"id": str(i), "count": 1} for i in b["items"]]}
                   for b in blocks],
    })
    data["itemSets"] = sets
    st, _ = lcu_put(path, data)
    return {"ok": st in (200, 201, 204), "status": st, "code": None if st in (200, 201, 204) else "rejected",
            "error": None if st in (200, 201, 204) else "Riot lehnte ab (Status %s)." % st}


# ---- Auto-Builds (wie Blitz/Porofessor): beim Locken automatisch übernehmen ----
# Sobald der eigene Pick gelockt ist, holt der Dienst den beliebtesten Build
# gegen den (geschätzten) Lane-Gegner von rankyoinker.de und schreibt Runen,
# Zauber und Item-Set in den Client - auch ohne offenes Overlay. Lockt der
# Lane-Gegner erst später, wird nachgezogen. Abschaltbar ("autoBuild"), und wer
# im Overlay selbst einen Build übernimmt, wird in diesem Spiel nicht mehr
# überschrieben.

LOL_MATCHUP_URL = "https://rankyoinker.de/api/lol/matchup"
# Ohne eigenen User-Agent ("Python-urllib/x.y") antwortet Cloudflare mit 403 -
# deshalb kam in dev8-dev12 nie ein Auto-Build an (Telemetrie: kein einziges
# lol_build_auto). Gleiches Muster wie Heartbeat/Client-Report.
LOL_BUILDS_HEADERS = {"User-Agent": "RankYoinker-Builds"}
LOL_ROLES_URL = "https://rankyoinker.de/api/lol/roles"
LOL_LANES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
# ARAM, Clash-ARAM, Arena, ARAM Mayhem: andere Builds, die Statistik passt nicht
LOL_AUTOBUILD_SKIP_QUEUES = {450, 720, 1700, 1710, 2400}
_lol_autobuild = {"applied": None, "manualGame": None, "last": None}
_lol_mu_cache = {}
_lol_roles_cache = {"ts": 0.0, "data": None}
_LOL_BLOCK_NAMES = {
    "de": ("Start", "Build-Reihenfolge", "Situativ"), "en": ("Starter", "Build order", "Situational"),
    "pl": ("Start", "Kolejność buildu", "Sytuacyjne"), "fr": ("Départ", "Ordre du build", "Situationnel"),
    "es": ("Inicio", "Orden del build", "Situacional"), "tr": ("Başlangıç", "Build sırası", "Duruma göre"),
    "ko": ("시작", "빌드 순서", "상황별"), "pt": ("Inicial", "Ordem da build", "Situacional"),
    "ru": ("Стартовые", "Порядок сборки", "Ситуативные"), "ja": ("スターター", "ビルド順", "状況に応じて"),
}
# Kaufweg-Blöcke (Komponenten -> fertiges Item); gleiche Titel wie im Overlay
_LOL_PATH_NAMES = {
    "de": ("Kaufweg Item {n}", "Kaufweg Stiefel"), "en": ("Path item {n}", "Path boots"),
    "pl": ("Ścieżka przedmiot {n}", "Ścieżka buty"), "fr": ("Parcours objet {n}", "Parcours bottes"),
    "es": ("Ruta objeto {n}", "Ruta botas"), "tr": ("Yol eşya {n}", "Yol botlar"),
    "ko": ("구매 경로 아이템 {n}", "구매 경로 신발"), "pt": ("Item do caminho {n}", "Botas do caminho"),
    "ru": ("Предмет {n}", "Ботинки"), "ja": ("ルートのアイテム{n}", "ルートのブーツ"),
}


def lol_set_auto_build(on):
    with _lol_cfg_lock:
        _lol_cfg["autoBuild"] = bool(on)
    _lol_cfg_save()
    return lol_auto_build_state()


def lol_auto_build_state():
    with _lol_cfg_lock:
        on = _lol_cfg.get("autoBuild", True)
    return {"ok": True, "autoBuild": on, "last": _lol_autobuild["last"]}


def _lol_roles():
    now = time.time()
    if _lol_roles_cache["data"] and now - _lol_roles_cache["ts"] < 12 * 3600:
        return _lol_roles_cache["data"]
    try:
        st, j = _http(LOL_ROLES_URL, LOL_BUILDS_HEADERS, timeout=8)
    except Exception as e:
        st, j = None, None
        report_error("lol-builds", e)
    if st not in (200, None):
        report_error("lol-builds", "roles HTTP %s" % st)
    if st == 200 and isinstance(j, dict) and j.get("ok"):
        roles = {}
        for k, v in (j.get("roles") or {}).items():
            if _pos_int(k) and isinstance(v, dict):
                roles[int(k)] = v
        _lol_roles_cache.update(ts=now, data=roles)
    return _lol_roles_cache["data"] or {}


def _lol_fetch_matchup(champ, vs, pos):
    key = (champ, vs or 0, pos)
    now = time.time()
    hit = _lol_mu_cache.get(key)
    if hit and now - hit[0] < (1800 if hit[1] else 120):
        return hit[1]
    url = "%s?champ=%d&pos=%s%s" % (LOL_MATCHUP_URL, champ, pos, "&vs=%d" % vs if vs else "")
    data = None
    try:
        st, j = _http(url, LOL_BUILDS_HEADERS, timeout=10)
        if st == 200 and isinstance(j, dict) and j.get("ok") and j.get("builds"):
            data = j
        elif st != 200:
            report_error("lol-builds", "matchup HTTP %s" % st)
    except Exception as e:
        data = None
        report_error("lol-builds", e)
    if len(_lol_mu_cache) > 300:
        _lol_mu_cache.clear()
    _lol_mu_cache[key] = (now, data)
    return data


def _lol_main_role(roles, champ):
    r = roles.get(champ) or {}
    return max(LOL_LANES, key=lambda l: r.get(l, 0))


def _lol_guess_opponent(my_pos, enemies, roles):
    """Wahrscheinlichste Verteilung der gelockten Gegner auf die Lanes - wer
    landet auf der eigenen? (gleiche Logik wie lolGuessOpponent() im Overlay)"""
    ids = [e for e in enemies if e][:5]
    if not ids or my_pos not in LOL_LANES:
        return None
    rate = lambda cid, lane: math.log((roles.get(cid) or {}).get(lane, 0) + 0.002)
    best, best_score = None, None
    for lanes in itertools.permutations(LOL_LANES, len(ids)):
        score = sum(rate(c, l) for c, l in zip(ids, lanes))
        if best_score is None or score > best_score:
            best, best_score = lanes, score
    return ids[best.index(my_pos)] if best and my_pos in best else None


def _lol_build_to_setup(name, b):
    lang = _heartbeat_lang()
    labels = _LOL_BLOCK_NAMES.get(lang, _LOL_BLOCK_NAMES["en"])
    path_names = _LOL_PATH_NAMES.get(lang, _LOL_PATH_NAMES["en"])
    path_blocks, n = [], 0
    for p in (b.get("path") or [])[:3]:
        if not isinstance(p, dict):
            continue
        if not p.get("boots"):
            n += 1
        path_blocks.append({"type": path_names[1] if p.get("boots") else path_names[0].format(n=n),
                            "items": list(p.get("steps") or [])})
    blocks = [
        {"type": labels[0], "items": b.get("start") or []},
        *path_blocks,
        # Volle Kaufreihenfolge (Server ab 2.4.2-dev9), sonst Kern + Stiefel
        {"type": labels[1], "items": b.get("order") or ((b.get("core") or []) + ([b["boots"]] if b.get("boots") else []))},
        {"type": labels[2], "items": b.get("situational") or []},
    ]
    return {"name": name, "spells": b.get("spells") if len(b.get("spells") or []) == 2 else [],
            "runes": b.get("runes"), "items": [x for x in blocks if x["items"]]}


def _lol_gameflow():
    try:
        st, j = lcu_get("/lol-gameflow/v1/session")
    except LcuError:
        return None
    return j if st == 200 and isinstance(j, dict) else None


def _lol_autobuild_tick():
    with _lol_cfg_lock:
        if not _lol_cfg.get("autoBuild", True):
            return
    try:
        st, j = lcu_get("/lol-champ-select/v1/session")
    except LcuError:
        return
    if st != 200 or not isinstance(j, dict):
        # Kein Nachziehen im laufenden Spiel: League lädt Item-Sets nur beim
        # Spielstart, eine Änderung danach kommt im Shop nicht an (Test 2026-10-12).
        return
    game = j.get("gameId")
    if _lol_autobuild["manualGame"] == game or j.get("queueId") in LOL_AUTOBUILD_SKIP_QUEUES:
        return
    local = j.get("localPlayerCellId")
    champs = _lol_cs_champions(j)
    mine = champs.get(local)
    if not mine or mine[1]:
        return                       # noch nicht gelockt
    champ = mine[0]
    me = next((c for c in (j.get("myTeam") or []) if c.get("cellId") == local), {})
    roles = _lol_roles()
    pos = (me.get("assignedPosition") or "").upper()
    if pos not in LOL_LANES:
        pos = _lol_main_role(roles, champ) if roles else "TOP"
    # Nur gelockte Gegner zählen. Ist ihre Rolle sichtbar (manche Modi), ist der
    # Lane-Gegner eindeutig, sonst wird geschätzt.
    locked = [(c, champs[c.get("cellId")][0]) for c in (j.get("theirTeam") or [])
              if champs.get(c.get("cellId")) and not champs[c.get("cellId")][1]]
    direct = next((cid for c, cid in locked if (c.get("assignedPosition") or "").upper() == pos), None)
    vs = direct or _lol_guess_opponent(pos, [cid for _, cid in locked], roles)
    key = (game, champ, vs, pos)
    if _lol_autobuild["applied"] == key:
        return
    data = _lol_fetch_matchup(champ, vs, pos)
    if not data:
        return                       # nächster Takt versucht es erneut (Fehlschlag kurz gecacht)
    _lol_autobuild["applied"] = key
    b = data["builds"][0]
    name = _lol_champion_catalog()["byId"].get(champ) or str(champ)
    res = lol_apply_build(champ, _lol_build_to_setup(name, b), manual=False)
    _lol_autobuild["last"] = {"gameId": game, "championId": champ, "vs": vs if not data.get("general") else None,
                              "guessedVs": vs, "pos": pos, "option": b.get("id"), "at": time.time(),
                              "ok": bool(res.get("ok")), "results": res.get("results") or {}, "error": res.get("error")}


# ---- Match-Verlauf (eigene puuid, lokale LCU-Historie — kein API-Key nötig) ----

def _lol_extract(game, puuid):
    me = None
    for ident in game.get("participantIdentities") or []:
        if (ident.get("player") or {}).get("puuid") == puuid:
            me = next((p for p in game.get("participants") or []
                       if p.get("participantId") == ident.get("participantId")), None)
            break
    if not me:
        return None
    st = me.get("stats") or {}
    kills, deaths, assists = st.get("kills") or 0, st.get("deaths") or 0, st.get("assists") or 0
    return {
        "gameId": game.get("gameId"), "queueId": game.get("queueId"),
        "startedAt": game.get("gameCreation"), "duration": game.get("gameDuration"),
        "championId": me.get("championId"), "won": bool(st.get("win")),
        "kills": kills, "deaths": deaths, "assists": assists,
        "kda": round((kills + assists) / (deaths or 1), 2),
        "cs": (st.get("totalMinionsKilled") or 0) + (st.get("neutralMinionsKilled") or 0),
        "gold": st.get("goldEarned"), "level": st.get("champLevel"),
        "visionScore": st.get("visionScore"),
    }


def lol_match_history(puuid, count=5):
    status, j = lcu_get("/lol-match-history/v1/products/lol/%s/matches?begIndex=0&endIndex=%d"
                        % (puuid, max(0, count - 1)))
    if status != 200 or not j:
        return {"ok": False, "error": "Verlauf nicht lesbar (Status %s)." % status, "matches": []}
    games = ((j.get("games") or {}).get("games") or [])[:count]
    matches = [m for m in (_lol_extract(g, puuid) for g in games) if m]
    return {"ok": True, "puuid": puuid, "matches": matches}


def lol_last_champion(puuid):
    """Champion aus dem letzten Match einer puuid, oder None. Fuer die Lobby-
    Ansicht: statt des Rang-Emblems als grosses Kartenbild zeigt eine Karte
    dort lieber den zuletzt gespielten Champion (der Rang steht sowieso schon
    separat in den Rang-Kacheln darunter)."""
    if not puuid:
        return None
    try:
        res = lol_match_history(puuid, count=1)
    except LcuError:
        return None
    matches = res.get("matches") or []
    return matches[0].get("championId") if matches else None


# ---- Saison-Übersicht (aggregiert aus derselben lokalen Historie) ----
# Kein eigener API-Aufruf nötig: einfach mehr Partien aus lol_match_history
# ziehen und zusammenfassen statt sie einzeln aufzulisten.

def lol_season_stats(puuid, count=20):
    status, j = lcu_get("/lol-match-history/v1/products/lol/%s/matches?begIndex=0&endIndex=%d"
                        % (puuid, max(0, count - 1)))
    if status != 200 or not j:
        return {"ok": False, "error": "Verlauf nicht lesbar (Status %s)." % status}
    games = ((j.get("games") or {}).get("games") or [])[:count]
    matches = [m for m in (_lol_extract(g, puuid) for g in games) if m]
    if not matches:
        return {"ok": True, "games": 0, "wins": 0, "losses": 0, "winrate": None,
                "avgKills": None, "avgDeaths": None, "avgAssists": None, "avgKda": None,
                "champs": []}

    n = len(matches)
    wins = sum(1 for m in matches if m["won"])
    avg = lambda key: round(sum(m[key] for m in matches) / n, 1)

    champ_stats = {}
    for m in matches:
        cid = m.get("championId")
        if not cid:
            continue
        cs = champ_stats.setdefault(cid, {"championId": cid, "games": 0, "wins": 0})
        cs["games"] += 1
        cs["wins"] += 1 if m["won"] else 0
    champs = sorted(champ_stats.values(), key=lambda c: c["games"], reverse=True)[:5]
    for c in champs:
        c["winrate"] = round(100 * c["wins"] / c["games"], 1)

    return {"ok": True, "games": n, "wins": wins, "losses": n - wins,
            "winrate": round(100 * wins / n, 1),
            "avgKills": avg("kills"), "avgDeaths": avg("deaths"), "avgAssists": avg("assists"),
            "avgKda": round(sum(m["kda"] for m in matches) / n, 2),
            "champs": champs}


# ============================ League-Match-Analyse (2.5.0-dev17) ============================
# Gegenstueck zur VALORANT-Analyse. Nach dem Spielende (Gameflow InProgress ->
# Nachspiel) holt der Dienst das Spiel aus der lokalen LCU-Historie
# (/lol-match-history/v1/games/<id>, dazu wenn vorhanden die Zeitleiste) und
# rechnet daraus die Auswertung: K/D/A, Kill-Beteiligung, Schaden, Gold, CS und
# Sicht je Spieler, Team-Ziele, Goldverlauf und eine Note relativ zur Lobby
# (1,00 = Schnitt, gleiche Schwellen wie bei VALORANT). Liegt in
# lol-match-report.json und gilt bis zum naechsten Spiel. Nur der lokale Client,
# keine Riot-Web-API.
LOL_MATCH_REPORT_FILE = os.path.join(DATA_DIR, "lol-match-report.json")
LOL_REPORT_RETRY = 20.0
LOL_REPORT_TRIES = 30            # ~10 min: die Historie kennt das Spiel erst nach einer Weile
LOL_REPORT_TL_TRIES = 6          # so oft auf die Zeitleiste warten, dann ohne sie
LOL_INGAME_PHASES = ("GameStart", "InProgress", "Reconnect")
_lol_report = {"current": None, "pending": None, "tries": 0, "next": 0.0, "data": None,
               "loaded": False, "busy": False,
               # Positionen aus der Live-Client-API, im Spiel gemerkt (siehe _lol_role)
               "positions": None, "posNext": 0.0, "posBusy": False}
_lol_report_lock = threading.Lock()


def _lol_report_get():
    with _lol_report_lock:
        if not _lol_report["loaded"]:
            _lol_report["loaded"] = True
            data = _load_json(LOL_MATCH_REPORT_FILE)
            if data.get("gameId"):
                _lol_report["data"] = data
        return _lol_report["data"]


def lol_match_report_state():
    data = _lol_report_get()
    return {"gameId": data.get("gameId") if data else None, "puuid": data.get("puuid") if data else None,
            "pending": bool(_lol_report["pending"])}


def _lol_report_drop(keep_game=None):
    data = _lol_report_get()
    with _lol_report_lock:
        if _lol_report["pending"] and _lol_report["pending"] != keep_game:
            _lol_report["pending"] = None
        if not data or (keep_game and data.get("gameId") == keep_game):
            return
        _lol_report["data"] = None
    for path in (LOL_MATCH_REPORT_FILE, LOL_MATCH_REPORT_FILE + ".tmp"):
        try:
            os.remove(path)
        except OSError:
            pass


def _lol_report_track(phase):
    """Aus dem League-Watcher, einmal je Sekunde: merkt sich die gameId des
    laufenden Spiels und plant nach dem Ende die Auswertung."""
    if phase == "ChampSelect":
        _lol_report_drop()                      # neues Spiel beginnt: alte Analyse weg
        return
    if phase in LOL_INGAME_PHASES:
        if not _lol_report["current"]:
            try:
                status, s = lcu_get("/lol-gameflow/v1/session")
                gid = ((s or {}).get("gameData") or {}).get("gameId") if status == 200 else None
            except LcuError:
                gid = None
            if gid:
                _lol_report["current"] = gid
                _lol_report_drop(keep_game=gid)
        gid = _lol_report["current"]
        pos = _lol_report["positions"]
        if gid and (not pos or pos.get("gameId") != gid) and not _lol_report["posBusy"] \
                and time.time() >= _lol_report["posNext"]:
            _lol_report.update(posBusy=True, posNext=time.time() + 10.0)

            def capture():
                try:
                    got = _lol_positions_capture(gid)
                    if got:
                        _lol_report["positions"] = got
                except Exception:
                    pass
                finally:
                    _lol_report["posBusy"] = False
            threading.Thread(target=capture, daemon=True).start()
        return
    gid = _lol_report["current"]
    if gid and phase:
        _lol_report["current"] = None
        with _lol_report_lock:
            _lol_report.update(pending=gid, tries=0, next=time.time() + 8.0)


def _lol_report_tick(now):
    with _lol_report_lock:
        gid = _lol_report["pending"]
        if not gid or _lol_report["busy"] or now < _lol_report["next"]:
            return
        _lol_report["busy"] = True
        attempt = _lol_report["tries"]

    def run():
        ok = False
        try:
            data = _lol_report_build(gid, attempt)
            if data:
                _save_json(LOL_MATCH_REPORT_FILE, data)
                with _lol_report_lock:
                    _lol_report.update(data=data, loaded=True)
                ok = True
        except Exception as e:
            report_error("lol-match-report", e)
        finally:
            with _lol_report_lock:
                _lol_report["busy"] = False
                if ok or _lol_report["tries"] + 1 >= LOL_REPORT_TRIES:
                    if _lol_report["pending"] == gid:
                        _lol_report["pending"] = None
                else:
                    _lol_report["tries"] += 1
                    _lol_report["next"] = time.time() + LOL_REPORT_RETRY

    threading.Thread(target=run, daemon=True).start()


def _lol_positions_capture(gid):
    """Rollen aller zehn aus der Live-Client-API (allPlayers[].position) - live
    (2026-10-16) die einzige verlaessliche Quelle; das LCU-Match-Protokoll nennt in
    participants[].timeline.lane z. B. Top-Spieler und Supporter als JUNGLE."""
    players = _live_get("/playerlist")
    if not isinstance(players, list) or not players:
        return None
    champs = _lol_champion_by_name()
    by_champ, by_riot = {}, {}
    for p in players:
        pos = str(p.get("position") or "").upper()
        if pos not in _LOL_POSITIONS:
            continue
        team = {"ORDER": 100, "CHAOS": 200}.get(str(p.get("team") or "").upper())
        cid = champs.get(str(p.get("championName") or "").lower())
        if team and cid:
            by_champ["%d:%d" % (team, cid)] = pos
        rid = str(p.get("riotId") or "")
        if rid and not rid.startswith("#"):
            by_riot[rid.lower()] = pos
    if not by_champ and not by_riot:
        return None                             # z. B. ARAM: keine Positionen
    return {"gameId": gid, "byChamp": by_champ, "byRiot": by_riot}


def _lol_report_build(game_id, attempt=0):
    status, game = lcu_get("/lol-match-history/v1/games/%s" % game_id)
    if status != 200 or not isinstance(game, dict) or not game.get("participants"):
        return None
    me = lol_current_summoner().get("puuid")
    tl = None
    try:
        st2, tl = lcu_get("/lol-match-history/v1/game-timelines/%s" % game_id)
        tl = tl if st2 == 200 and isinstance(tl, dict) and tl.get("frames") else None
    except LcuError:
        tl = None
    if tl is None and attempt < LOL_REPORT_TL_TRIES:
        return None                             # Zeitleiste kommt oft etwas spaeter
    pos = _lol_report["positions"]
    return _lol_report_from_game(game, tl, me, pos if pos and pos.get("gameId") == game_id else None)


_LOL_POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
# Gegenstand der Rollen-Quest (stats.roleBoundItem) - live in beiden Teams einheitlich;
# die Botlane bekommt statt eines Platzhalters gleich ihre Stiefel (IDs ab 3000)
# Supporter haben nach erfuellter Quest 2055 (live, Spiel 8014740439, 34 min)
_LOL_ROLE_ITEM = {1220: "TOP", 1209: "JUNGLE", 1206: "MIDDLE", 1208: "UTILITY", 2055: "UTILITY"}
_LOL_SMITE = 11
_LOL_LANE_ROLE = {"TOP": "TOP", "JUNGLE": "JUNGLE", "MIDDLE": "MIDDLE", "MID": "MIDDLE"}
# Gewichte der Note je Rolle: Supporter farmen nicht und machen wenig Schaden,
# dafuer zaehlen Sicht und Kill-Beteiligung mehr
_LOL_RATING_W = {"default": {"kda": .25, "kp": .15, "dpm": .25, "gpm": .15, "cspm": .10, "vpm": .10},
                 "UTILITY": {"kda": .25, "kp": .25, "dpm": .10, "gpm": .05, "cspm": 0.0, "vpm": .35}}
# Kein Einzelwert zaehlt mehr als das 3-Fache des Schnitts (sonst traegt ein Ausreisser die ganze Note)
_LOL_RATIO_CAP = 3.0


def _lol_role(p, pl=None, positions=None):
    """Rolle in drei Stufen: im Spiel gemerkte Position (Live-Client-API), sonst das
    Rollen-Quest-Item, sonst lane/role aus dem Match-Protokoll - dort steht live
    oft JUNGLE fuer Top-Spieler und Supporter, darum zaehlt JUNGLE nur mit Smite."""
    if positions:
        rid = ("%s#%s" % (pl.get("gameName"), pl.get("tagLine"))).lower() if pl and pl.get("gameName") and pl.get("tagLine") else ""
        pos = (positions.get("byRiot") or {}).get(rid) \
            or (positions.get("byChamp") or {}).get("%s:%s" % (p.get("teamId"), p.get("championId")))
        if pos in _LOL_POSITIONS:
            return pos
    try:
        rbi = int((p.get("stats") or {}).get("roleBoundItem") or 0)
    except (TypeError, ValueError):
        rbi = 0
    if rbi in _LOL_ROLE_ITEM:
        return _LOL_ROLE_ITEM[rbi]
    if rbi >= 3000:
        return "BOTTOM"
    if _LOL_SMITE in (p.get("spell1Id"), p.get("spell2Id")):
        return "JUNGLE"
    tl = p.get("timeline") or {}
    lane, role = str(tl.get("lane") or "").upper(), str(tl.get("role") or "").upper()
    if lane in ("BOTTOM", "BOT"):
        return "UTILITY" if "SUPPORT" in role else "BOTTOM"
    if lane == "JUNGLE":
        return None                             # ohne Smite kein Jungler
    return _LOL_LANE_ROLE.get(lane)


def _lol_report_from_game(game, tl, me_puuid, positions=None):
    idents = {i.get("participantId"): (i.get("player") or {}) for i in (game.get("participantIdentities") or [])}
    parts = [p for p in (game.get("participants") or []) if p.get("participantId") in idents]
    me_pid = next((pid for pid, pl in idents.items() if me_puuid and pl.get("puuid") == me_puuid), None)
    teams_seen = sorted({p.get("teamId") for p in parts})
    if me_pid is None or len(teams_seen) != 2:
        return None                             # nur klassische 5-gegen-5-Spiele (kein Arena)
    me_p = next(p for p in parts if p.get("participantId") == me_pid)
    my_team = me_p.get("teamId")
    enemy_team = next(t for t in teams_seen if t != my_team)
    dur = max(60, int(game.get("gameDuration") or 0))
    if dur > 20000:                             # manche Clients liefern Millisekunden
        dur //= 1000
    mins = dur / 60.0
    team_kills, team_dmg = collections.Counter(), collections.Counter()
    for p in parts:
        st = p.get("stats") or {}
        team_kills[p.get("teamId")] += st.get("kills") or 0
        team_dmg[p.get("teamId")] += st.get("totalDamageDealtToChampions") or 0
    players = []
    for p in parts:
        st, pl = p.get("stats") or {}, idents.get(p.get("participantId")) or {}
        k, d, a = st.get("kills") or 0, st.get("deaths") or 0, st.get("assists") or 0
        cs = (st.get("totalMinionsKilled") or 0) + (st.get("neutralMinionsKilled") or 0)
        tid = p.get("teamId")
        gn = pl.get("gameName") or pl.get("summonerName") or ""
        dmg = st.get("totalDamageDealtToChampions") or 0
        players.append({
            "pid": p.get("participantId"), "puuid": pl.get("puuid"),
            "name": ("%s#%s" % (gn, pl.get("tagLine"))) if gn and pl.get("tagLine") else (gn or None),
            "me": p.get("participantId") == me_pid, "ally": tid == my_team, "team": tid,
            "championId": p.get("championId"), "level": st.get("champLevel"), "role": _lol_role(p, pl, positions),
            "kills": k, "deaths": d, "assists": a, "kda": round((k + a) / (d or 1), 2),
            "kp": round((k + a) / team_kills[tid] * 100) if team_kills[tid] else 0,
            "cs": cs, "csMin": round(cs / mins, 1), "gold": st.get("goldEarned") or 0,
            "goldMin": round((st.get("goldEarned") or 0) / mins), "dmg": dmg,
            "dmgMin": round(dmg / mins), "dmgShare": round(dmg / team_dmg[tid] * 100) if team_dmg[tid] else 0,
            "dmgTaken": st.get("totalDamageTaken") or 0, "vision": st.get("visionScore") or 0,
            "visionMin": round((st.get("visionScore") or 0) / mins, 2),
            "wards": st.get("wardsPlaced") or 0, "wardsKilled": st.get("wardsKilled") or 0,
            "controlWards": st.get("visionWardsBoughtInGame") or 0,
            "items": [st.get("item%d" % i) or 0 for i in range(7)],
            "spells": [p.get("spell1Id"), p.get("spell2Id")], "keystone": st.get("perk0"),
            "subStyle": st.get("perkSubStyle"),
            "multi": st.get("largestMultiKill") or 0, "doubles": st.get("doubleKills") or 0,
            "triples": st.get("tripleKills") or 0, "quadras": st.get("quadraKills") or 0,
            "pentas": st.get("pentaKills") or 0, "firstBlood": bool(st.get("firstBloodKill")),
            "turrets": st.get("turretKills") or 0, "win": bool(st.get("win")),
        })
    # Note relativ zur Lobby
    metric = {"kda": lambda x: (x["kills"] + x["assists"]) / (x["deaths"] + 1), "kp": lambda x: x["kp"],
              "dpm": lambda x: x["dmgMin"], "gpm": lambda x: x["goldMin"], "cspm": lambda x: x["csMin"],
              "vpm": lambda x: x["visionMin"]}
    avg = {m: sum(f(x) for x in players) / len(players) for m, f in metric.items()}
    # Sicht gegen dieselbe Rolle: Supporter warden von Berufs wegen mehr - gegen den
    # Lobby-Schnitt wurde live ein Supporter mit 0/9/9 Team-MVP (Sicht 2,1x = 0,74 Punkte)
    vis_by_role = collections.defaultdict(list)
    for x in players:
        if x["role"]:
            vis_by_role[x["role"]].append(x["visionMin"])
    for x in players:
        w = _LOL_RATING_W.get(x["role"]) or _LOL_RATING_W["default"]
        base = dict(avg)
        peers = vis_by_role.get(x["role"]) or []
        if len(peers) >= 2 and sum(peers) > 0:
            base["vpm"] = sum(peers) / len(peers)
        parts_ = [(w[m], min(_LOL_RATIO_CAP, metric[m](x) / base[m])) for m in metric if w[m] and base[m]]
        ws = sum(wt for wt, _ in parts_)
        x["rating"] = round(sum(wt * v for wt, v in parts_) / ws, 2) if ws else None
    ranked = sorted([x for x in players if x["rating"] is not None], key=lambda x: -x["rating"])
    if ranked:
        ranked[0]["mvp"] = "match"
        for t in teams_seen:                    # Team-MVP nur im anderen Team (siehe VALORANT)
            best = next((x for x in ranked if x["team"] == t), None)
            if best and best is not ranked[0]:
                best["mvp"] = "team"
    players.sort(key=lambda x: (not x["ally"], ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY", None].index(x["role"])))

    def team_obj(tid):
        t = next((t for t in (game.get("teams") or []) if t.get("teamId") == tid), {}) or {}
        return {"kills": team_kills[tid], "towers": t.get("towerKills") or 0, "inhibs": t.get("inhibitorKills") or 0,
                "dragons": t.get("dragonKills") or 0, "barons": t.get("baronKills") or 0,
                "heralds": t.get("riftHeraldKills") or 0, "grubs": t.get("hordeKills"),
                "firstBlood": bool(t.get("firstBlood")), "firstTower": bool(t.get("firstTower")),
                "firstDragon": bool(t.get("firstDargon") or t.get("firstDragon")), "firstBaron": bool(t.get("firstBaron")),
                "win": str(t.get("win") or "").lower() in ("win", "true")}
    ally_o, enemy_o = team_obj(my_team), team_obj(enemy_team)
    me = next(x for x in players if x["me"])
    result = "win" if me["win"] else "loss"
    if dur < 300 and not ally_o["towers"] and not enemy_o["towers"]:
        result = "remake"

    highlights = []
    if me.get("mvp"):
        highlights.append({"type": "mvp", "kind": me["mvp"]})
    for key, n in (("pentas", 5), ("quadras", 4), ("triples", 3)):
        if me[key]:
            highlights.append({"type": "multi", "n": n, "count": me[key]})
            break
    if me["firstBlood"]:
        highlights.append({"type": "firstblood"})
    if me["dmg"] and me["dmg"] == max(x["dmg"] for x in players):
        highlights.append({"type": "topdamage", "value": me["dmg"]})
    if me["vision"] and me["vision"] == max(x["vision"] for x in players):
        highlights.append({"type": "topvision", "value": me["vision"]})
    if me["cs"] and me["cs"] == max(x["cs"] for x in players):
        highlights.append({"type": "topcs", "value": me["cs"]})
    if me["deaths"] == 0 and mins >= 15:
        highlights.append({"type": "deathless"})

    return {
        "v": 1, "game": "lol", "gameId": game.get("gameId"), "puuid": me_puuid, "createdAt": int(time.time()),
        "queueId": game.get("queueId"), "mode": game.get("gameMode"), "mapId": game.get("mapId"),
        "startedAt": game.get("gameCreation"), "duration": dur, "result": result,
        "score": {"ally": ally_o["kills"], "enemy": enemy_o["kills"]},
        "teams": {"ally": ally_o, "enemy": enemy_o},
        "players": players, "highlights": highlights[:8],
        "timeline": _lol_report_timeline(tl, idents, parts, my_team, me_pid) if tl else None,
    }


def _lol_report_timeline(tl, idents, parts, my_team, me_pid):
    """Je Minute: Goldvorsprung des eigenen Teams, eigenes Gold/CS und was passiert
    ist (Drachen, Baron, Herold, Tuerme, Inhibitoren, eigene Kills/Tode)."""
    team_of = {p.get("participantId"): p.get("teamId") for p in parts}
    out = []
    for f in tl.get("frames") or []:
        pf = f.get("participantFrames") or {}
        ally = enemy = 0
        mine = {}
        for key, v in pf.items():
            try:
                pid = int(v.get("participantId") or key)
            except (TypeError, ValueError):
                continue
            g = v.get("totalGold") or 0
            if team_of.get(pid) == my_team:
                ally += g
            else:
                enemy += g
            if pid == me_pid:
                mine = v
        ev = []
        mk = md = ma = 0
        for e in f.get("events") or []:
            typ = e.get("type")
            if typ == "CHAMPION_KILL":
                if e.get("killerId") == me_pid:
                    mk += 1
                if e.get("victimId") == me_pid:
                    md += 1
                if me_pid in (e.get("assistingParticipantIds") or []):
                    ma += 1
            elif typ == "ELITE_MONSTER_KILL":
                mt = str(e.get("monsterType") or "").upper()
                kind = {"DRAGON": "dragon", "BARON_NASHOR": "baron", "RIFTHERALD": "herald", "HORDE": "grub"}.get(mt)
                # killerTeamId fehlt im LCU (live immer None) -> Team des Killers;
                # killerId 0 / unbekannt: gar nicht zuordnen statt "Gegner"
                killer_team = e.get("killerTeamId") or team_of.get(e.get("killerId"))
                if kind and killer_team in (100, 200):
                    ev.append({"t": kind, "side": "ally" if killer_team == my_team else "enemy"})
            elif typ == "BUILDING_KILL":
                kind = {"TOWER_BUILDING": "tower", "INHIBITOR_BUILDING": "inhib"}.get(str(e.get("buildingType") or ""))
                lost_team = e.get("teamId")
                if kind and lost_team:
                    ev.append({"t": kind, "side": "enemy" if lost_team == my_team else "ally"})
        row = {"m": int(round((f.get("timestamp") or 0) / 60000.0)), "lead": ally - enemy,
               "gold": mine.get("totalGold") or 0,
               "cs": (mine.get("minionsKilled") or 0) + (mine.get("jungleMinionsKilled") or 0),
               "lvl": mine.get("level") or 0, "k": mk, "d": md, "a": ma, "ev": ev}
        # Der letzte Frame ist das Spielende (live 34:00 und 34:25 -> zweimal "34"):
        # in dieselbe Minute zusammenfassen, Stand = der spaetere
        if out and row["m"] <= out[-1]["m"]:
            prev = out[-1]
            for key in ("k", "d", "a"):
                row[key] += prev[key]
            row["ev"] = prev["ev"] + row["ev"]
            row["m"] = prev["m"]
            out[-1] = row
        else:
            out.append(row)
    return out


# ---- Champion-Katalog (Name -> numerische ID, für Portraits) ----
# Die Live-Client-API unten kennt nur Champion-NAMEN, keine IDs — Community
# Dragon (Riots eigenes, öffentliches CDN für Spieldaten-Assets, kein Login
# nötig) liefert einmal täglich die Zuordnung, daraus baut sich dann die
# Portrait-URL (".../champion/{id}/splash-art/centered").

CHAMPION_SUMMARY_URL = ("https://raw.communitydragon.org/latest/plugins/"
                        "rcp-be-lol-game-data/global/default/v1/champion-summary.json")
_lol_champs = {"ts": 0.0, "byName": {}, "byId": {}}


def _lol_champion_catalog():
    now = time.time()
    if now - _lol_champs["ts"] < 86400 and _lol_champs["byName"]:
        return _lol_champs
    try:
        # Ohne Browser-User-Agent antwortet Community Dragon mit 403 (blockt den
        # Standard-urllib-Header) — gleicher Trick wie beim VALORANT-Riot-Client oben.
        status, j = _http(CHAMPION_SUMMARY_URL,
                          {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
                          timeout=10)
        if status == 200 and isinstance(j, list):
            # Manche Einträge sind Spielmodus-Varianten desselben Namens (z. B.
            # Arena) mit einer zusätzlichen, viel grösseren ID — bei doppeltem
            # Namen gewinnt darum immer die kleinere (die "echte") ID.
            by_name, display = {}, {}
            for c in j:
                name, cid = c.get("name"), c.get("id")
                if not name or not isinstance(cid, int) or cid < 0:
                    continue
                key = name.lower()
                if key not in by_name or cid < by_name[key]:
                    by_name[key] = cid
                    display[key] = name        # Original-Schreibweise ("Kai'Sa", "Dr. Mundo", ...)
            by_id = {cid: display[key] for key, cid in by_name.items()}
            _lol_champs.update({"byName": by_name, "byId": by_id, "ts": now})
    except Exception:
        pass
    return _lol_champs


def _lol_champion_by_name():
    return _lol_champion_catalog()["byName"]


def lol_champion_list():
    cat = _lol_champion_catalog()
    champs = [{"id": cid, "name": name} for cid, name in cat["byId"].items()]
    champs.sort(key=lambda c: c["name"])
    return {"ok": True, "champions": champs}


# ---- Champion Mastery (nur die eigene — die LCU gibt Mastery-Werte anderer
# Spieler grundsätzlich nicht her, das bräuchte den offiziellen Riot-API-Key) ----

def lol_champion_mastery(champion_id):
    if not champion_id:
        return None
    try:
        status, j = lcu_get("/lol-champion-mastery/v1/local-player/champion-mastery/%s" % champion_id)
    except LcuError:
        return None
    if status != 200 or not j:
        return None
    return {"level": j.get("championLevel"), "points": j.get("championPoints")}


# ---- Live-Daten während einer laufenden Partie (Port 2999, ohne Auth) ----
# Komplett unabhängig von der LCU: Diese API gibt es nur, solange eine Partie
# WIRKLICH läuft, dafür ganz ohne Anmeldung — jedes Programm auf dem PC darf
# sie während eines Matches abfragen (macht z. B. auch der Spiel-Client selbst
# für manche Overlays).

LIVE_URL = "https://127.0.0.1:2999/liveclientdata"


def _live_get(path):
    try:
        status, j = _http(LIVE_URL + path, {}, timeout=4, local_unverified=True)
    except Exception:
        return None
    return j if status == 200 else None


def _lol_live_puuid_map():
    """championId -> puuid, direkt aus der Gameflow-Sitzung. Zuverlaessiger als
    der Namens-Abgleich ueber die Live-Client-API: klappt auch bei Leerzeichen/
    Sonderzeichen im Namen (das liess vorher manche echten Spieler ohne Rang
    dastehen) und liefert sogar fuer Bots eine puuid."""
    try:
        status, j = lcu_get("/lol-gameflow/v1/session")
    except LcuError:
        return {}
    if status != 200 or not j:
        return {}
    sels = (j.get("gameData") or {}).get("playerChampionSelections") or []
    return {s.get("championId"): s.get("puuid") for s in sels if s.get("championId") and s.get("puuid")}


def _lol_live_squad_map():
    """puuid -> Arena-Teilteam-ID (teamParticipantId, meist 3 Spieler je Team).
    Die Live-Client-API kennt fuer Arena nur die zwei technischen Alt-Teams
    ORDER/CHAOS (Restbestand der 2-Team-Engine) — die tatsaechliche Gruppierung
    in kleine Teams steht nur in der Gameflow-Sitzung. Team-Groessen koennen je
    nach Arena-Variante wechseln (2er, 3er, ...), daher keine feste Zahl hier."""
    try:
        status, j = lcu_get("/lol-gameflow/v1/session")
    except LcuError:
        return {}
    if status != 200 or not j:
        return {}
    game_data = j.get("gameData") or {}
    out = {}
    for entry in (game_data.get("teamOne") or []) + (game_data.get("teamTwo") or []):
        puuid, tid = entry.get("puuid"), entry.get("teamParticipantId")
        if puuid and tid is not None and puuid not in out:
            out[puuid] = tid
    return out


def lol_live_game():
    active = _live_get("/activeplayer")
    players = _live_get("/playerlist")
    if active is None or players is None:
        return {"ok": False, "error": "Keine laufende Partie erreichbar."}

    # Beobachtet in Arena: die Live-Client-API liefert denselben Spieler
    # (identischer riotId + Champion) manchmal doppelt in derselben Antwort.
    # Ohne Entduplizierung baute die Seite bei jedem Poll eine weitere
    # Geisterkarte für genau diesen Spieler.
    seen, deduped = set(), []
    for p in players:
        key = (p.get("riotId") or "", p.get("championName") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    players = deduped

    me_id = active.get("riotId") or active.get("summonerName")
    champs = _lol_champion_by_name()
    puuid_by_champ = _lol_live_puuid_map()
    # Arena ("Cherry") hat mehr als zwei Teams — nur dort überhaupt nach der
    # Teilteam-Zuordnung fragen, das ist ein zusätzlicher LCU-Aufruf.
    stats = _live_get("/gamestats") or {}
    is_arena = stats.get("gameMode") == "CHERRY"
    squads = _lol_live_squad_map() if is_arena else {}

    # "isBot" aus der Live-Client-API ist NICHT verlässlich — echte Spieler mit
    # verstecktem Namen ("privater Modus") kommen genauso mit riotId "#" und
    # isBot=false daher wie tatsächliche Bots. Die Gameflow-Sitzung kennt aber
    # in beiden Fällen die puuid; darum wird der Rang IMMER versucht (fehlt er
    # wirklich, bleibt die Kachel bei "Unranked" — kein Sonderfall nötig).
    puuids = [puuid_by_champ.get(champs.get((p.get("championName") or "").lower()))
             for p in (players or [])]
    puuids = [pu for pu in puuids if pu]
    ranks = dict(zip(puuids, _pool.map(lol_ranked, puuids))) if puuids else {}

    out = []
    for p in players or []:
        sc = p.get("scores") or {}
        is_bot = bool(p.get("isBot"))
        champ_name = p.get("championName")
        champ_id = champs.get((champ_name or "").lower())
        riot_id = p.get("riotId") or ""
        hidden = not riot_id or riot_id == "#"
        if is_bot:
            pid = (champ_name or "Bot") + " (Bot)"
        elif hidden:
            # Name versteckt (Bot oder private Anzeige) — Riot liefert hier meist
            # den Champion-Namen als Platzhalter, das reicht als Anzeige.
            pid = p.get("summonerName") or champ_name or "Unbekannt"
        else:
            pid = riot_id
        puuid = puuid_by_champ.get(champ_id)
        r = ranks.get(puuid) or {}
        out.append({
            "name": pid, "puuid": puuid, "champion": champ_name, "isBot": is_bot,
            "championId": champ_id,
            "team": p.get("team"), "position": (p.get("position") or "").upper() or None,
            "level": p.get("level"), "isDead": bool(p.get("isDead")),
            "kills": sc.get("kills"), "deaths": sc.get("deaths"),
            "assists": sc.get("assists"), "cs": sc.get("creepScore"),
            "me": (not hidden) and riot_id == me_id,
            "rankSolo": r.get("RANKED_SOLO_5x5"), "rankFlex": r.get("RANKED_FLEX_SR"),
            "squad": squads.get(puuid) if is_arena else None,
        })
    my_champ_id = next((p["championId"] for p in out if p["me"]), None)
    # gameTime (Sekunden) und gameMode für den Kopf der Ansicht (Spieluhr, Kartenname)
    return {"ok": True, "players": out, "gold": active.get("currentGold"),
            "level": active.get("level"), "myMastery": lol_champion_mastery(my_champ_id),
            "arena": is_arena, "gameTime": stats.get("gameTime"), "gameMode": stats.get("gameMode")}


# ---- Begegnungen (eigenes League-Konto) ----
# Gleiches Prinzip wie bei VALORANT oben (siehe encounters_record), aber in
# einer eigenen Datei und mit einer eigenen "Wer bin ich" — die LCU kennt kein
# get_auth()/Lockfile, die puuid kommt hier von lol_current_summoner(). Statt
# einer Karte (die es bei League nicht gibt) wird die Warteschlange notiert.

LOL_ENCOUNTERS_FILE = os.path.join(DATA_DIR, "vry_lol_encounters.json")
_lol_enc_lock = threading.Lock()
LOL_ENC_LOG_MAX = 20


def _lol_enc_account():
    me = lol_current_summoner().get("puuid")
    root = _load_json(LOL_ENCOUNTERS_FILE)
    acc = root.get(me)
    if not isinstance(acc, dict):
        acc = {"players": {}, "matches": []}
        root[me] = acc
    acc.setdefault("players", {})
    acc.setdefault("matches", [])
    return root, acc, me


def lol_encounters_for(puuids):
    try:
        with _lol_enc_lock:
            _, acc, _ = _lol_enc_account()
            out = {p: acc["players"][p] for p in puuids if p in acc["players"]}
        return {"ok": True, "players": out}
    except LcuError as e:
        return {"ok": False, "error": str(e), "players": {}}


def lol_encounters_record(match_id, players, meta=None):
    if not match_id or not isinstance(players, list):
        return {"ok": False, "error": "match_id oder Spielerliste fehlt."}
    meta = meta or {}
    try:
        with _lol_enc_lock:
            root, acc, me = _lol_enc_account()
            if match_id in acc["matches"]:
                return {"ok": True, "skipped": True}
            acc["matches"].append(match_id)
            acc["matches"] = acc["matches"][-400:]
            now = int(time.time())
            for p in players:
                pid = p.get("puuid")
                if not pid or pid == me:
                    continue
                e = acc["players"].setdefault(pid, {"games": 0, "ally": 0, "enemy": 0})
                e["games"] = e.get("games", 0) + 1
                ally = bool(p.get("ally"))
                if ally:
                    e["ally"] = e.get("ally", 0) + 1
                else:
                    e["enemy"] = e.get("enemy", 0) + 1
                if p.get("name"):
                    e["name"] = p["name"]
                e["last"] = now
                entry = {"t": now, "a": 1 if ally else 0}
                if meta.get("queue"):
                    entry["q"] = meta["queue"]
                log = e.setdefault("log", [])
                log.append(entry)
                e["log"] = log[-LOL_ENC_LOG_MAX:]
            _save_json(LOL_ENCOUNTERS_FILE, root)
        return {"ok": True}
    except LcuError as e:
        return {"ok": False, "error": str(e)}


_lol_enc_seen = {"match": None}


def _lol_record_encounter_now():
    """Begegnungen der laufenden Partie zaehlen — ganz ohne offene Seite,
    genau wie record_encounter_now() bei VALORANT. Braucht sowohl die
    Gameflow-Sitzung (fuer eine stabile gameId) als auch die Live-Client-API
    (fuer das tatsaechliche Team-Ergebnis), darum erst ab INGAME sinnvoll."""
    try:
        status, gf = lcu_get("/lol-gameflow/v1/session")
    except LcuError:
        return False
    if status != 200 or not gf:
        return False
    game_data = gf.get("gameData") or {}
    game_id = game_data.get("gameId")
    if not game_id:
        return False

    _notify_match_tracked_once("league", game_id)

    if _lol_enc_seen["match"] == game_id:
        return False

    live = lol_live_game()
    if not live.get("ok"):
        return False
    my_team = next((p.get("team") for p in live["players"] if p.get("me")), None)
    players = [{"puuid": p["puuid"],
                "ally": (p.get("team") == my_team) if my_team else True,
                "name": p.get("name")}
               for p in live["players"] if p.get("puuid") and not p.get("me")]
    if not players:
        return False

    queue_id = (game_data.get("queue") or {}).get("id")
    queue_name = _lol_queue_catalog()["map"].get(queue_id) if queue_id else None
    res = lol_encounters_record(str(game_id), players, {"queue": queue_name})
    _lol_enc_seen["match"] = game_id
    return bool(res.get("ok")) and not res.get("skipped")


# ============================ HTTP ============================

# Antworten werden nur für die eigene Seite freigegeben, nicht mehr für jede
# beliebige Website. Vom Handy läuft alles ohnehin same-origin.
# "rankyoinker" gehoert dazu: install_all.py traegt http://rankyoinker/ in die hosts-Datei
# ein, und Tray/Desktop-Symbol (overlay_url) oeffnen genau diese Adresse. Fehlte bis
# 2.5.0-dev5 - von dort wurde jeder POST (Instalock, Agent waehlen, Update ...) abgelehnt.
_ORIGIN_OK = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\]|vry|rankyoinker)(:\d+)?$", re.I)
_HOST_NAMES_OK = {"localhost", "127.0.0.1", "[::1]", "vry", "rankyoinker"}


class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, *args):
        pass

    def _cors(self):
        """Freigabe nur fuer die eigene Seite. 'null' (file://, aber auch jede
        Webseite mit sandboxed iframe) bekommt seit 2.5.3 keine Freigabe mehr."""
        origin = self.headers.get("Origin")
        if not origin:
            return
        if _ORIGIN_OK.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def _send(self, code, body):
        data = body.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    # ---------------------------- Zugriffsschutz ----------------------------

    def _client_ip(self):
        ip = self.client_address[0] if self.client_address else ""
        return ip[7:] if ip.startswith("::ffff:") else ip

    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, val = part.strip().partition("=")
            if key == name:
                return urllib.parse.unquote(val)
        return ""

    def _origin_ok_silent(self):
        """Same-Origin-Pruefung OHNE eigene Fehlerantwort (Aufrufer entscheidet
        selbst, was/ob geantwortet wird - siehe _origin_trusted() und
        _authorized() unten). _is_local() (Quell-IP) allein reicht nicht als
        Schutz: eine ganz normale, im selben Browser geoeffnete fremde
        Webseite kann per fetch()/Formular problemlos einen POST gegen
        http://localhost:1101 schicken - der kommt technisch genauso von
        127.0.0.1 wie ein Aufruf von unserer eigenen Seite. _cors() half
        hier nicht: das steuert nur, ob der Aufrufer die ANTWORT lesen darf,
        nicht ob der Request ueberhaupt ausgefuehrt wird. Ohne diese Pruefung
        liess sich z.B. /api/rankyoinker/self-update von JEDER geoeffneten
        Webseite aus missbrauchen, praktisch Remote Code Execution
        (Community-Sicherheitsbericht, 2026-09-28).
        Erlaubt: kein Origin-Header (kommt von einem echten Browser-POST
        praktisch nie vor, nur von curl/Tools) und unsere eigenen Origins
        (_ORIGIN_OK - localhost/127.0.0.1/vry/rankyoinker, jeder Port)."""
        origin = self.headers.get("Origin")
        # "null" (Seite per file:// geoeffnet) war bis 2.5.2 erlaubt - den Origin kann aber
        # JEDE Webseite erzeugen (sandboxed iframe, data:-URL) und damit den Dienst
        # fernsteuern und auslesen (Sicherheitspruefung 2026-10-17). Seit 2.5.3 nicht mehr.
        return (not origin) or bool(_ORIGIN_OK.match(origin))

    def _host_ok(self):
        """Gegen DNS-Rebinding (seit 2.5.3): Eine fremde Webseite kann ihren eigenen
        Namen auf 127.0.0.1 zeigen lassen - ihre GET-Anfragen kommen dann ohne Origin
        vom PC selbst und galten als eigene Seite. Erlaubt sind nur unsere Namen und
        IP-Adressen (die lassen sich nicht per DNS umbiegen; das Handy kommt per IP)."""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True                      # kein Browser (Browser schicken immer Host)
        if host.startswith("["):
            name = host.split("]", 1)[0] + "]"
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if name in _HOST_NAMES_OK:
            return True
        try:
            ipaddress.ip_address(name.strip("[]"))
            return True
        except ValueError:
            return False

    def _host_check(self):
        if self._host_ok():
            return True
        self.close_connection = True
        self._json({"ok": False, "error": "Unbekannter Host - abgelehnt."}, 403)
        return False

    def _origin_trusted(self):
        if self._origin_ok_silent():
            return True
        self._json({"ok": False, "error": "Anfrage nicht von dieser Seite - abgelehnt."}, 403)
        return False

    def _ovl_check(self):
        """Nur das Spiel-Overlay (game_overlay.py): vom PC selbst und mit dem Token, das der
        Dienst ihm beim Start in DATA_DIR/game-overlay.json mitgibt."""
        token = self.headers.get("X-RY-Ovl") or ""
        if _is_local(self._client_ip()) and token and hmac.compare_digest(token, _ovl["token"]):
            return True
        self._json({"ok": False, "error": "forbidden"}, 403)
        return False

    def _local_only(self):
        """Kopplung verwalten und sicherheitskritische PC-Aktionen (Self-
        Update!) duerfen nur vom PC selbst UND von unserer eigenen Seite aus
        kommen — sonst koennte sich ein gekoppeltes Handy weitere Geraete
        dazuholen, oder schlimmer, jede beliebige im Browser offene
        Webseite den PC fernsteuern (siehe _origin_ok_silent())."""
        if not _is_local(self._client_ip()):
            self._json({"ok": False, "error": "Nur am PC selbst möglich."}, 403)
            return False
        return self._origin_trusted()

    def _authorized(self):
        """Fuer LOKALE Aufrufer zusaetzlich zur IP auch die Herkunft pruefen
        (siehe _origin_ok_silent()) - sonst koennte jede im Browser offene
        fremde Webseite genau wie unsere eigene Seite Aktionen ausloesen
        (Instalock, Loadout aendern, Dodge, ...). Gekoppelte Handys sind
        davon nicht betroffen: die authentifizieren sich ueber das
        Kopplungs-Cookie (pair_check), nicht ueber die Quell-IP, und deren
        Origin ist ohnehin die tatsaechliche LAN-Adresse, nicht localhost."""
        if _is_local(self._client_ip()):
            return self._origin_ok_silent()
        return pair_check(self._cookie(PAIR_COOKIE), self._client_ip())

    def _deny(self, msg, page):
        """Fremdes Geraet ohne Kopplung: Seite bekommt einen Hinweis, die
        Schnittstellen ein sauberes 403."""
        if not page:
            self._json({"ok": False, "error": msg, "needsPairing": True}, 403)
            return
        # Kein %-Formatieren: im CSS stecken Prozentzeichen
        html = (
            "<!doctype html><html lang=de><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>RankYoinker — nicht gekoppelt</title>"
            "<style>html,body{height:100%;margin:0}body{display:flex;align-items:center;"
            "justify-content:center;background:#0e1015;color:#e6e8ef;"
            "font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;padding:24px;"
            "text-align:center}div{max-width:420px}h1{font-size:20px;margin:0 0 10px}"
            "p{margin:0;color:#9aa0b0}</style>"
            "<div><h1>Dieses Gerät ist nicht gekoppelt</h1><p>"
            + msg.replace("&", "&amp;").replace("<", "&lt;")
            + "</p></div>"
        )
        data = html.encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _safe(self, fn):
        """Ruft eine Riot-Funktion auf und verpackt Fehler als saubere JSON-Antwort."""
        try:
            self._json(fn())
        except RiotError as e:
            self._json({"ok": False, "error": str(e)})
        except Exception as e:
            report_error("api " + urllib.parse.urlparse(self.path).path, e)
            self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def do_OPTIONS(self):
        # Der Vorab-Check des Browsers darf nicht an der Kopplung scheitern
        if not self._host_check():
            return
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_check():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > POST_MAX_BYTES:             # nichts davon lesen (Speicher), Verbindung zu
            self.close_connection = True
            self._json({"ok": False, "error": "too_large"}, 413)
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw) if raw and path != "/api/share/upload" else {}
        except Exception:
            body = {}

        if not self._authorized():
            self._deny("Koppel es am PC neu über „Handy verbinden“.", False)
            return

        if path == "/api/pair/start":
            if self._local_only():
                self._safe(lambda: pair_start(body.get("host")))
        elif path == "/api/pair/cancel":
            if self._local_only():
                self._safe(pair_cancel)
        elif path == "/api/pair/revoke":
            if self._local_only():
                self._safe(lambda: pair_revoke(body.get("id"), bool(body.get("all"))))

        elif path.startswith("/api/party/"):
            action = path.rsplit("/", 1)[-1]
            self._safe(lambda: party_action(action, body))
        elif path == "/api/problem-report":
            self._safe(lambda: problem_report_send(body.get("text"), body.get("contact"), bool(body.get("logs"))))
        elif path == "/api/dodge":
            self._safe(dodge_pregame)
        elif path == "/api/share/upload":
            q = urllib.parse.parse_qs(parsed.query)
            self._safe(lambda: share_upload(raw, (q.get("game") or [""])[0], (q.get("match") or [""])[0]))
        elif path == "/api/agent/select":
            self._safe(lambda: agent_action("select", body.get("agent")))
        elif path == "/api/agent/lock":
            self._safe(lambda: agent_action("lock", body.get("agent")))
        elif path == "/api/loadout/apply":
            self._safe(lambda: put_loadout(body))
        elif path == "/api/set-lang":
            local = _is_local(self._client_ip())
            self._safe(lambda: set_rpc_lang(body.get("lang"), auto=bool(body.get("auto")), local=local))
        elif path == "/api/set-perf":
            local = _is_local(self._client_ip())
            self._safe(lambda: set_perf_mode(body.get("on"), local=local))
        elif path == "/api/presets/save":
            self._safe(lambda: preset_save(body.get("name"), body.get("data")))
        elif path == "/api/presets/delete":
            self._safe(lambda: preset_delete(body.get("name")))
        elif path == "/api/presets/import":
            self._safe(lambda: preset_import(body.get("presets")))
        elif path == "/api/encounters/record":
            self._safe(lambda: encounters_record(body.get("matchId"), body.get("players")))
        elif path == "/api/instalock/arm":
            self._safe(lambda: instalock_arm(body))
        elif path == "/api/instalock/disarm":
            self._safe(instalock_disarm)
        elif path == "/api/instalock/answer":
            self._safe(lambda: instalock_answer(str(body.get("answer") or "")))
        elif path == "/api/ovl/action":
            if self._ovl_check():
                self._safe(lambda: ovl_action(body))
        elif path == "/api/ovl/settings":
            if self._local_only():
                self._safe(lambda: set_ovl_settings(body))
        elif path == "/api/ovl/preview":
            if self._local_only():
                self._safe(ovl_preview)

        elif path == "/api/lol/queue/setQueue":
            self._safe(lambda: lol_queue_action("setQueue", body))
        elif path == "/api/lol/queue/roles":
            self._safe(lambda: lol_queue_action("roles", body))
        elif path == "/api/lol/queue/search":
            self._safe(lambda: lol_queue_action("search"))
        elif path == "/api/lol/queue/cancel":
            self._safe(lambda: lol_queue_action("cancel"))
        elif path == "/api/lol/queue/leave":
            self._safe(lambda: lol_queue_action("leave"))
        elif path == "/api/lol/readycheck/accept":
            self._safe(lol_accept_ready_check)
        elif path == "/api/lol/autoaccept":
            self._safe(lambda: lol_set_auto_accept(body.get("on", True)))
        elif path == "/api/lol/champselect/select":
            self._safe(lambda: lol_champ_action(body.get("actionId"), body.get("championId"), False))
        elif path == "/api/lol/champselect/lock":
            self._safe(lambda: lol_champ_action(body.get("actionId"), body.get("championId"), True))
        elif path == "/api/lol/presets/ban":
            self._safe(lambda: lol_presets_set_ban(body.get("position"), body.get("slot", 0),
                                                     body.get("championId"), body.get("name")))
        elif path == "/api/lol/presets/pick":
            self._safe(lambda: lol_presets_set_pick(body.get("position"), body.get("slot", 0),
                                                     body.get("championId"), body.get("name")))
        elif path == "/api/lol/autobuild":
            self._safe(lambda: lol_set_auto_build(body.get("on", True)))
        elif path == "/api/lol/build/apply":
            self._safe(lambda: lol_apply_build(body.get("championId"), body.get("setup")))
        elif path == "/api/lol/presets/auto":
            self._safe(lambda: lol_presets_set_auto(body.get("ban"), body.get("pick")))

        elif path == "/api/rankyoinker/self-update":
            # Nur vom PC selbst UND von unserer eigenen Seite - nicht über ein
            # gekoppeltes Handy auslösbar (_local_only()). Nimmt bewusst KEINE
            # downloadUrl/sha256 mehr aus dem Request entgegen - siehe
            # trigger_self_update()/_resolve_official_update(): die holt
            # beides selbst per eigenem HTTPS-Aufruf, damit der Aufrufer
            # (frueher: einfach das Request-Body) nicht mehr bestimmen kann,
            # welche Datei als "verifiziert" gilt.
            if self._local_only():
                self._safe(trigger_self_update)

        # /start, /shutdown, /stop waren frueher GET-Endpunkte - fuer den
        # lokalen PC-Aufrufer inzwischen egal (der braucht seit dem 2026-09-28
        # Fix ohnehin einen vertrauenswuerdigen Origin), aber fuer ein
        # gekoppeltes Handy pruefte _authorized() hier nur das Pairing-Cookie,
        # ohne Origin-Check. Das Cookie ist SameSite=Lax, wird also auch bei
        # einer normalen (Top-Level-)Navigation zu einer fremden Seite noch
        # mitgeschickt - als GET reichte z.B. ein simples <img src="http://
        # <im-LAN-erreichbarer-PC>:1101/shutdown"> auf einer beliebigen
        # Webseite, um den PC des gekoppelten Handys stumm herunterzufahren.
        # Als POST verschickt kein Browser das Cookie mehr cross-site (Lax
        # deckt nur Top-Level-GET-Navigationen ab), was genau diese Luecke
        # schliesst (Community-Sicherheitsbericht, 2026-09-29).
        elif path == "/start":
            res = start_vry()
            res.setdefault("ok", True)
            res["running"] = vry_running() or res.get("running", False)
            self._json(res)
        elif path == "/shutdown":
            # Alles beenden, auch dieser Dienst (nur über stop_vry.bat)
            self._json({"ok": True, "running": False, "shuttingDown": True})
            try:
                self.wfile.flush()
            except Exception:
                pass
            stop_everything()
        elif path == "/stop":
            res = stop_vry_only() or {}
            res.setdefault("ok", True)
            res["running"] = vry_running()
            self._json(res)

        else:
            self._json({"ok": False, "error": "unbekannter Endpunkt"}, 404)

    def _send_page(self):
        """Liefert index.html aus, damit die Seite unter http://localhost:1101
        (bzw. http://vry) erreichbar ist statt ueber einen file://-Pfad."""
        path = os.path.join(BASE, "index.html")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except Exception:
            self._json({"ok": False, "error": "index.html nicht gefunden"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_lines(self):
        path = newest_log()
        if not path:
            return "", []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return os.path.basename(path), fh.readlines()
        except Exception as e:
            return os.path.basename(path), ["[Fehler beim Lesen der Log-Datei: %s]" % e]

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        is_page = path in ("/", "/index.html", "/vry")
        if not self._host_check():
            return

        # Lesezeichen-Symbol ist harmlos und muss auch ohne Kopplung kommen -
        # iOS holt das Home-Bildschirm-Icon teils ohne Cookies.
        if path in _APP_ICONS:
            data = base64.b64decode(_APP_ICONS[path])
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # Start-Link einer Handy-Verknuepfung aus 2.5.0-dev2..dev14 (start_url des
        # damaligen Manifests): Token pruefen, als Cookie setzen.
        if q.get("d") and is_page:
            token = q["d"][0]
            if not pair_check(token, self._client_ip()):
                self._deny("Diese App-Verknüpfung gilt nicht mehr (Gerät entkoppelt). "
                           "Kopple das Handy am PC neu und füge die App danach erneut hinzu.", True)
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie",
                             "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
                             % (PAIR_COOKIE, token, PAIR_MAX_AGE))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # QR-Code eingescannt: Code einlösen, Token als Cookie setzen und die
        # Adresse gleich wieder säubern, damit der Code nicht im Verlauf landet.
        if q.get("p"):
            token = pair_claim(q["p"][0], self.headers.get("User-Agent"), self._client_ip())
            if not token:
                self._deny("Der Kopplungscode ist abgelaufen oder wurde schon benutzt. "
                           "Erzeuge am PC einen neuen.", is_page)
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie",
                             "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
                             % (PAIR_COOKIE, token, PAIR_MAX_AGE))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not self._authorized():
            self._deny("Scanne am PC den QR-Code unter „Handy verbinden“, "
                       "dann darf dieses Gerät mitlesen.", is_page)
            return

        if is_page:
            self._send_page()

        elif path == "/img":
            got = img_get((q.get("u") or [""])[0])
            if not got:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            else:
                data, ctype = got
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.send_header("X-Content-Type-Options", "nosniff")
                # oeffentliche Bilder - darf jede Herkunft ins Canvas zeichnen (auch file://)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

        elif path == "/favicon.ico":
            self._send(204, "")

        elif path == "/api/languages":
            # Liste der verfuegbaren Sprachen = einfach die Dateinamen in
            # lang/ - siehe LANG_DIR oben. Neue Sprache = neue Datei, kein
            # Code hier muss dafuer angefasst werden.
            try:
                codes = sorted(
                    f[:-5] for f in os.listdir(LANG_DIR)
                    if f.endswith(".json") and _LANG_CODE_RE.match(f[:-5])
                )
            except Exception:
                codes = []
            self._json({"ok": True, "codes": codes})

        elif path.startswith("/lang/") and path.endswith(".json"):
            code = path[len("/lang/"):-len(".json")]
            if not _LANG_CODE_RE.match(code):
                self._json({"ok": False, "error": "ungueltiger Sprachcode"}, 400)
            else:
                try:
                    with open(os.path.join(LANG_DIR, code + ".json"), "r", encoding="utf-8") as fh:
                        self._send(200, fh.read())
                except Exception:
                    self._json({"ok": False, "error": "Sprachdatei nicht gefunden"}, 404)

        elif path == "/api/pair":
            if self._local_only():
                self._safe(pair_state)

        elif path.startswith("/log"):
            name, lines = self._read_lines()
            out = {"file": name, "log": _redact_log("".join(lines[-500:])),
                   "progress": parse_progress(lines)}
            out.update(proc_state())
            self._json(out)

        elif path.startswith("/status"):
            _page_seen["ts"] = time.time()    # Seite offen -> Vorladen lohnt sich
            _, lines = self._read_lines()
            # Der Instalock kommt hier bewusst mit: Startet der Dienst neu und
            # nimmt ein Scharfschalten wieder auf, merkt die Seite das sonst
            # nicht — sie fragt /api/instalock nur, solange sie es selbst kennt.
            with _il_lock:
                il = {"on": bool(_il["on"]), "gen": _il["gen"]}
            out = {"appVersion": APP_VERSION,
                   "progress": parse_progress(lines), "game": game_state(),
                   "instalock": il, "activeGame": detect_active_game(),
                   "lol": lol_gamestate()}
            out.update(proc_state())
            out.update(account_state())
            self._json(out)

        elif path == "/api/local-config":
            # Liest ausschliesslich fuer den Update-Check gedachte, lokale
            # Einstellungen aus config.json (siehe checkForUpdate() in
            # index.html) - "updateChannel" ("stable"/"dev") - und seit 2.5.4
            # "perfMode" (true/false, null = noch nie gesetzt, siehe set_perf_mode()).
            # Kein Riot-Zugriff noetig, darum ganz oben und ohne try/RiotError.
            try:
                data = _load_json(CONFIG_JSON_FILE)
                channel = str((data or {}).get("updateChannel", "stable")).lower()
                perf = data.get("perfMode")
            except Exception:
                channel, perf = "stable", None
            self._json({"ok": True, "updateChannel": channel if channel == "dev" else "stable",
                        "perfMode": perf if isinstance(perf, bool) else None,
                        # Spiel-Overlay (2.5.5-dev2): Fenster ueber VALORANT, nur unter Windows
                        "gameOverlay": ovl_settings(), "gameOverlayAvailable": ovl_available()})

        elif path == "/api/session":
            try:
                a = get_auth()
                self._json({"ok": True, "puuid": a["puuid"], "shard": a["shard"],
                            "pod": a["pod"], "version": a.get("version"),
                            "gen": _account["gen"]})
            except RiotError as e:
                self._json({"ok": False, "error": str(e)})
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        elif path == "/api/problem-report/preview":
            # Zeigt vorab, was "Problem melden" schicken wuerde (gleiche Daten)
            diag, log = problem_diagnostics(q.get("logs", ["0"])[0] == "1")
            self._json({"ok": True, "diag": diag, "log": log})

        elif path == "/api/match-report":
            data = _report_current()
            self._json({"ok": bool(data), "report": data, "pending": bool(_report["pending"])})

        elif path == "/api/kda":
            puuids = [p for p in (q.get("puuids", [""])[0]).split(",") if p]
            if not puuids:
                self._json({"ok": False, "error": "keine puuids angegeben"})
                return
            try:
                get_auth()
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
                return
            # Die Seite fragt je Spieler einzeln (Kachel erscheint, sobald ihre
            # Daten da sind) - dann direkt in diesem Thread statt ueber den Pool
            rows = ([card_data(puuids[0])] if len(puuids) == 1
                    else list(_pool.map(card_data, puuids[:12])))
            self._json({"ok": True, "players": {r["puuid"]: r for r in rows}})

        elif path == "/api/party":
            self._safe(party_info)

        elif path == "/api/lobby":
            # wie /api/party, zusätzlich Rang/RR/Peak für die Lobby-Karten
            self._safe(lobby_info)

        elif path == "/api/agents/owned":
            self._safe(lambda: {"ok": True, "agents": owned_agents()})

        elif path == "/api/entitlements":
            self._safe(entitlements)

        elif path == "/api/loadout":
            self._safe(lambda: get_loadout(cached=q.get("cached", ["0"])[0] == "1"))

        elif path == "/api/shop":
            force = (q.get("force", ["0"])[0] or "0") not in ("0", "", "false")
            self._safe(lambda: get_shop(force))

        elif path == "/api/presets":
            self._safe(account_presets)

        elif path == "/api/encounters":
            ids = [p for p in (q.get("puuids", [""])[0]).split(",") if p]
            self._safe(lambda: encounters_for(ids))

        elif path == "/api/premades":
            # Wer im Match mit wem zusammen in einer Party ist — auch Fremde
            self._safe(premades)

        elif path == "/api/matchid":
            self._safe(current_match_id)

        elif path == "/api/instalock":
            self._json(instalock_state())

        elif path == "/api/ovl/state":
            if self._ovl_check():
                self._safe(ovl_state)

        elif path == "/api/gamestate":
            # Was Riot sagt — unabhängig davon, was vry.exe gerade anzeigt
            self._json(dict(game_state(), ok=True))

        elif path == "/api/rrhistory":
            puuid = q.get("puuid", [""])[0]
            try:
                n = max(3, min(20, int(q.get("n", ["15"])[0])))
            except ValueError:
                n = 15
            # Ohne Puuid (manche Heartbeat-Eintraege sind nur nach Namen
            # verschluesselt) kam hier frueher der Spielername an und landete
            # roh in der Riot-URL -> InvalidURL / UnicodeEncodeError.
            self._safe(lambda: rr_history(puuid, n) if _is_puuid(puuid)
                       else {"ok": False, "error": "Keine Spieler-ID verfügbar."})

        elif path == "/api/player":
            puuid = q.get("puuid", [""])[0]
            try:
                count = max(1, min(50, int(q.get("n", ["5"])[0])))
            except ValueError:
                count = 5
            if not _is_puuid(puuid):
                self._json({"ok": False, "error": "Keine Spieler-ID verfügbar."})
                return
            data = player_stats(puuid, count)
            data["ok"] = "error" not in data
            self._json(data)

        elif path == "/api/pregame":
            try:
                d = pregame_state()
                if d.get("ok") and d.get("matchId"):
                    # erst hier ausrechnen - pregame_state() ist bis zu 0,7 s gecacht
                    d = dict(d, lockIn=round(manual_lock_wait(d["matchId"]), 1))
                self._json(d)
            except RiotError as e:
                self._json({"ok": False, "error": str(e)})
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        elif path == "/api/dodge":
            # Seit 2.5.3 nur per POST: als GET reichte ein <img src=".../api/dodge"> auf
            # einer beliebigen Webseite, um den Nutzer aus der Agentenauswahl zu werfen
            self._json({"ok": False, "error": "Nur per POST."}, 405)

        elif path == "/api/live":
            # Live-Daten von vry.exe fuers Handy (seit 2.5.3): der WebSocket von vry.exe
            # ist nur noch am PC selbst erreichbar, das Handy holt sie hier - mit Kopplung
            try:
                since = int((q.get("since") or ["-1"])[0])
                wait = float((q.get("wait") or ["0"])[0])
            except ValueError:
                since, wait = -1, 0.0
            self._json(live_state(since, wait))

        elif path == "/api/activegame":
            self._json(detect_active_game())

        elif path == "/api/lol/session":
            try:
                self._json(lol_session())
            except LcuError as e:
                self._json({"ok": False, "error": str(e)})
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        elif path == "/api/lol/gamestate":
            out = lol_gamestate()
            out["report"] = lol_match_report_state()
            self._json(out)

        elif path == "/api/lol/match-report":
            data = _lol_report_get()
            self._json({"ok": bool(data), "report": data, "pending": bool(_lol_report["pending"])})

        elif path == "/api/lol/lobby":
            self._json(lol_lobby())

        elif path == "/api/lol/searchstate":
            self._json(lol_search_state())

        elif path == "/api/lol/autobuild":
            self._json(lol_auto_build_state())
        elif path == "/api/lol/readycheck":
            self._json(lol_ready_check())

        elif path == "/api/lol/champselect":
            self._json(lol_champ_select())

        elif path == "/api/lol/ranked":
            puuid = q.get("puuid", [""])[0]
            if not puuid:
                self._json({"ok": False, "error": "puuid fehlt"})
                return
            self._json({"ok": True, "puuid": puuid, "ranked": lol_ranked(puuid)})

        elif path == "/api/lol/matches":
            puuid = q.get("puuid", [""])[0]
            try:
                count = max(1, min(10, int(q.get("n", ["5"])[0])))
            except ValueError:
                count = 5
            if not puuid:
                self._json({"ok": False, "error": "puuid fehlt"})
                return
            self._json(lol_match_history(puuid, count))

        elif path == "/api/lol/seasonstats":
            puuid = q.get("puuid", [""])[0]
            try:
                count = max(1, min(30, int(q.get("n", ["20"])[0])))
            except ValueError:
                count = 20
            if not puuid:
                self._json({"ok": False, "error": "puuid fehlt"})
                return
            self._json(lol_season_stats(puuid, count))

        elif path == "/api/lol/live":
            self._json(lol_live_game())

        elif path == "/api/lol/queues":
            self._json(lol_queue_list())

        elif path == "/api/lol/champions":
            self._json(lol_champion_list())

        elif path == "/api/lol/encounters":
            ids = [p for p in (q.get("puuids", [""])[0]).split(",") if p]
            self._safe(lambda: lol_encounters_for(ids))

        elif path == "/api/lol/presets":
            self._json(lol_presets_state())



        else:
            self._json({}, 404)


# ============================ System-Tray-Icon ============================
# Reines ctypes/Win32 statt eines Pakets wie pystray+Pillow - vermeidet eine
# zusaetzliche, binaerlastige Abhaengigkeit im Installer (RankYoinker bringt
# bewusst nur das eingebettete Python + wenige reine Python-Pakete mit, siehe
# python310._pth). Shell_NotifyIcon + eine klassische Win32-Nachrichtenschleife
# sind gut dokumentierte, seit Jahrzehnten stabile APIs.
#
# Laeuft in einem eigenen Daemon-Thread (siehe Aufruf unten in __main__) -
# bewusst NICHT der Haupt-Thread, damit ein Fehler hier (z.B. auf einer
# ungewoehnlichen Windows-Konfiguration) niemals den eigentlichen Log-Server
# mitreisst. Jede Win32-Interaktion ist entsprechend defensiv in try/except
# gekapselt und geloggt statt den Prozess abstuerzen zu lassen.

_TRAY_ICON_PATH = os.path.join(BASE, "rankyoinker.ico")
_TRAY_CLASS_NAME = "RankYoinkerTrayWndClass"
_WM_TRAYICON = 0x8001  # WM_APP + 1 - eigene Nachricht fuer Tray-Klicks
_ID_MENU_OPEN = 1001
_ID_MENU_QUIT = 1002


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_void_p),
        ("lParam", ctypes.c_void_p),
        ("time", ctypes.c_uint),
        ("pt", _POINT),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersionOrTimeout", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


def _tray_setup_prototypes(user32, shell32, kernel32):
    """Explizite argtypes/restype fuer alle hier genutzten Win32-Funktionen.

    Ohne das behandelt ctypes Rueckgabewerte/Zeigerargumente standardmaessig
    als 32-bit c_int - auf 64-bit Windows sind Handles (HWND, HICON, HMENU, ...)
    aber 64-bit Zeiger. Ohne diese Deklarationen koennten hohe Handle-Werte
    beim Rueckgabe- oder Weiterreichen still abgeschnitten werden, ein
    Fehlerbild, das erst bei bestimmten Speicheradressen auftritt und darum
    lokal kaum zu reproduzieren waere. Einmalig beim Start des Tray-Threads
    aufgerufen."""
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    user32.CreatePopupMenu.restype = ctypes.c_void_p
    user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_wchar_p]
    user32.TrackPopupMenu.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.DestroyMenu.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.c_void_p]


_TRAY_STRINGS = {
    "de": {"open": "RankYoinker öffnen", "quit": "Programm schließen"},
    "en": {"open": "Open RankYoinker", "quit": "Quit program"},
    "pl": {"open": "Otwórz RankYoinker", "quit": "Zamknij program"},
    "fr": {"open": "Ouvrir RankYoinker", "quit": "Quitter le programme"},
    "es": {"open": "Abrir RankYoinker", "quit": "Cerrar programa"},
    "tr": {"open": "RankYoinker'ı aç", "quit": "Programı kapat"},
    "ko": {"open": "RankYoinker 열기", "quit": "프로그램 종료"},
    "pt": {"open": "Abrir RankYoinker", "quit": "Fechar programa"},
    "ru": {"open": "Открыть RankYoinker", "quit": "Закрыть программу"},
    "ja": {"open": "RankYoinkerを開く", "quit": "プログラムを終了"},
}


def _tray_strings():
    """Tray-Menuetexte in der eingestellten App-Sprache - waren bisher fest
    auf Deutsch verdrahtet, unabhaengig davon, was in den Einstellungen
    gewaehlt war. Gleiche Quelle wie Discord Rich Presence (config.json
    "lang", siehe _heartbeat_lang())."""
    return _TRAY_STRINGS.get(_heartbeat_lang(), _TRAY_STRINGS["de"])


def _tray_open_overlay():
    threading.Thread(target=open_overlay_in_browser, daemon=True).start()


def _tray_wndproc_factory(user32, shell32, nid):
    """nid wird hier nur PER REFERENZ gehalten (ctypes-Structures sind
    veraenderlich) - beim Aufruf ist es noch leer, wird aber laengst
    vollstaendig befuellt sein, bevor tatsaechlich eine erste Windows-
    Nachricht eintrifft (siehe Reihenfolge in _run_tray_icon)."""
    # LRESULT ist zeigergross (64-bit auf x64) - c_long waere hier nur 32-bit
    # und koennte auf 64-bit Windows zu einer falschen Calling-Convention-
    # Breite fuer den Rueckgabewert fuehren.
    WNDPROCTYPE = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)

    def _show_menu(hwnd):
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            MF_STRING = 0x00000000
            tray_strings = _tray_strings()
            user32.AppendMenuW(hmenu, MF_STRING, _ID_MENU_OPEN, tray_strings["open"])
            user32.AppendMenuW(hmenu, MF_STRING, _ID_MENU_QUIT, tray_strings["quit"])
            pt = _POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # Notwendig, damit sich das Menue bei einem Klick daneben wieder
            # schliesst - ohne das bleibt es haengen (bekannter Win32-Trick,
            # siehe MSDN-Doku zu TrackPopupMenu).
            user32.SetForegroundWindow(hwnd)
            TPM_BOTTOMALIGN = 0x0020
            TPM_LEFTALIGN = 0x0000
            user32.TrackPopupMenu(hmenu, TPM_LEFTALIGN | TPM_BOTTOMALIGN, pt.x, pt.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, 0, 0, 0)  # WM_NULL - schliesst das Menue zuverlaessig
        finally:
            user32.DestroyMenu(hmenu)

    def _wndproc(hwnd, msg, wparam, lparam):
        WM_DESTROY = 0x0002
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205
        WM_COMMAND = 0x0111
        NIM_DELETE = 0x00000002

        try:
            if msg == _WM_TRAYICON:
                if lparam == WM_LBUTTONDBLCLK:
                    _tray_open_overlay()
                elif lparam == WM_RBUTTONUP:
                    _show_menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == _ID_MENU_OPEN:
                    _tray_open_overlay()
                elif cmd == _ID_MENU_QUIT:
                    try:
                        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                    except Exception:
                        pass
                    stop_everything()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            _ulog("Tray: Fehler in wndproc (msg=%s): %s: %s" % (msg, type(e).__name__, e))
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    return WNDPROCTYPE(_wndproc)


def _run_tray_icon():
    """Registriert das Tray-Icon und laesst eine klassische Win32-
    Nachrichtenschleife laufen, bis das Programm beendet wird. Rein
    ergaenzendes Komfort-Feature - jeder Fehler hier wird geloggt und
    bricht nur diesen Thread ab, nie den eigentlichen Log-Server."""
    try:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        _tray_setup_prototypes(user32, shell32, kernel32)

        if not os.path.exists(_TRAY_ICON_PATH):
            _ulog("Tray: Icon-Datei fehlt (%s), Tray-Icon uebersprungen." % _TRAY_ICON_PATH)
            return

        hinstance = kernel32.GetModuleHandleW(None)

        # nid wird erst nach CreateWindowExW vollstaendig befuellt (braucht
        # hwnd), aber der WndProc-Callback wird schon jetzt gebaut - er haelt
        # nur eine Referenz darauf, siehe Docstring von _tray_wndproc_factory.
        nid = _NOTIFYICONDATAW()
        # Referenz MUSS am Leben bleiben, solange das Fenster existiert - sonst
        # sammelt der Garbage Collector den Callback ein und Windows ruft
        # danach in ungueltigen Speicher. Diese Funktion kehrt erst zurueck,
        # wenn die Nachrichtenschleife (weiter unten) endet, haelt die
        # Referenz also automatisch fuer die gesamte Laufzeit.
        real_wndproc = _tray_wndproc_factory(user32, shell32, nid)

        wndclass = _WNDCLASSW()
        wndclass.style = 0
        wndclass.lpfnWndProc = ctypes.cast(real_wndproc, ctypes.c_void_p)
        wndclass.cbClsExtra = 0
        wndclass.cbWndExtra = 0
        wndclass.hInstance = hinstance
        wndclass.hIcon = None
        wndclass.hCursor = None
        wndclass.hbrBackground = None
        wndclass.lpszMenuName = None
        wndclass.lpszClassName = _TRAY_CLASS_NAME

        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            _ulog("Tray: RegisterClassW fehlgeschlagen, GetLastError=%s" % ctypes.GetLastError())
            return

        WS_OVERLAPPEDWINDOW = 0x00CF0000
        hwnd = user32.CreateWindowExW(
            0, _TRAY_CLASS_NAME, "RankYoinker", WS_OVERLAPPEDWINDOW,
            0, 0, 0, 0, None, None, hinstance, None,
        )
        if not hwnd:
            _ulog("Tray: CreateWindowExW fehlgeschlagen, GetLastError=%s" % ctypes.GetLastError())
            return
        # Fenster bleibt bewusst unsichtbar - existiert nur, damit Windows
        # Nachrichten an es zustellen kann (Tray-Klicks, Menue-Auswahl).

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        hicon = user32.LoadImageW(None, _TRAY_ICON_PATH, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not hicon:
            _ulog("Tray: LoadImageW fehlgeschlagen, GetLastError=%s" % ctypes.GetLastError())
            return

        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        NIF_MESSAGE = 0x00000001
        NIF_ICON = 0x00000002
        NIF_TIP = 0x00000004
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = _WM_TRAYICON
        nid.hIcon = hicon
        nid.szTip = "RankYoinker"

        NIM_ADD = 0x00000000
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            _ulog("Tray: Shell_NotifyIconW(NIM_ADD) fehlgeschlagen, GetLastError=%s" % ctypes.GetLastError())
            return

        _ulog("Tray: Icon eingerichtet, Nachrichtenschleife startet.")
        msg = _MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception as e:
        _ulog("Tray: unerwarteter Fehler, Tray-Icon deaktiviert: %s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    if "--open-app" in sys.argv[1:]:
        # Nur die Seite im Browser oeffnen (open_app.vbs) - den laufenden Dienst NICHT
        # beenden, darum vor _kill_other_log_servers()
        raise SystemExit(0 if _open_app_cli() else 1)
    _kill_other_log_servers()  # Selbstheilung: egal wie gestartet, nie zwei Instanzen
    try:
        # Auf allen Netzwerkkarten hören, damit auch das Handy im selben Netz
        # drankommt. Fremde Geräte kommen nur mit Kopplung durch (siehe oben).
        #
        # WICHTIG: http.server.HTTPServer setzt allow_reuse_address=1 (für
        # POSIX, um TIME_WAIT nach einem Neustart zu vermeiden) - unter
        # Windows erlaubt SO_REUSEADDR aber oft, dass ein ZWEITER Prozess
        # denselben Port zusätzlich bindet, OHNE Fehler. Genau darauf
        # verlässt sich aber die "except OSError" unten ("Port belegt also
        # läuft schon eine Instanz") - ohne das explizit abzuschalten griff
        # dieses Sicherheitsnetz unter Windows nie zuverlässig, und zwei
        # Instanzen liefen parallel weiter, ohne dass es je einen Fehler gab.
        ThreadingHTTPServer.allow_reuse_address = False
        main_server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        raise SystemExit(0)  # Port belegt -> Instanz laeuft schon
    main_server.daemon_threads = True

    # Lief das letzte Selbst-Update wirklich durch? (sonst Telemetrie-Fehler)
    check_pending_update()

    # Wacht über vry.exe: genau eine Instanz, Neustart nach einem Absturz
    threading.Thread(target=_watchdog, daemon=True).start()
    # Erkennt einen Account-Wechsel und wirft dann alle Caches weg
    threading.Thread(target=_account_watcher, daemon=True).start()
    threading.Thread(target=_account_warmup, daemon=True).start()
    # Verfolgt den echten Spielzustand und holt vry.exe zurück, wenn es ein
    # laufendes Match verpasst hat
    try:
        _digests_load()                         # abgeschlossene Matches aus frueheren Laeufen
    except Exception as e:
        report_error("match-digests", e)
    if os.path.exists(MATCH_SNAPSHOT_FILE):
        threading.Thread(target=_snapshot_restore, daemon=True).start()   # als Erstes: gehoert er zum Match?
    else:
        _snapshot_ready.set()
    threading.Thread(target=_game_watcher, daemon=True).start()
    # Instalock: läuft hier statt im Browser, damit ein Hintergrund-Tab die
    # Verzögerung nicht verschleppt. Ein frisches Scharfschalten überlebt
    # einen Neustart des Dienstes.
    _il_restore()
    threading.Thread(target=_instalock_watcher, daemon=True).start()
    # Spiel-Overlay (game_overlay.py): laeuft, solange VALORANT offen ist
    threading.Thread(target=_ovl_watcher, daemon=True).start()
    # League of Legends: automatisches Annehmen der Spielfindung, unabhängig
    # vom VALORANT-Zweig oben — gleiches Prinzip wie der Instalock.
    _lol_cfg_restore()
    _lol_presets_restore()
    threading.Thread(target=_lol_watcher, daemon=True).start()
    threading.Thread(target=_lol_fast_watcher, daemon=True).start()
    # RankYoinker: anonymer Aktiv-Nutzer-Ping für die Website (siehe oben)
    threading.Thread(target=_active_user_heartbeat, daemon=True).start()
    # RankYoinker: Account+Feature-Bericht alle 5 Minuten fürs Admin-Dashboard
    threading.Thread(target=_client_report_loop, daemon=True).start()
    # System-Tray-Icon (Doppelklick öffnet die Seite, Rechtsklick zeigt ein
    # Menü) - siehe _run_tray_icon weiter oben für die Begründung, warum das
    # in einem eigenen Thread statt im Haupt-Thread läuft.
    threading.Thread(target=_run_tray_icon, daemon=True).start()

    # Zusätzlich Port 80, damit die Seite unter http://vry erreichbar ist.
    # Ist er belegt, läuft trotzdem alles über Port 1101 weiter.
    for extra_port in (80,):
        try:
            extra = ThreadingHTTPServer(("0.0.0.0", extra_port), Handler)
        except OSError:
            continue
        extra.daemon_threads = True
        threading.Thread(target=extra.serve_forever, daemon=True).start()

    main_server.serve_forever()
