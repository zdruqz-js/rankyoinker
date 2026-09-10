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
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(BASE, "logs")
PORT = 1101
CREATE_NO_WINDOW = 0x08000000

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
LOCKFILE = os.path.join(LOCALAPPDATA, "Riot Games", "Riot Client", "Config", "lockfile")
SHOOTER_LOG = os.path.join(LOCALAPPDATA, "VALORANT", "Saved", "Logs", "ShooterGame.log")

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
# Discord /stats grobe Versions-/Sprachverteilung sehen kann. Es wird dabei
# nichts weiter als eine zufällige, pro Installation einmalig erzeugte ID plus
# App-Version und UI-Sprache übertragen - keine Account-, Spiel- oder
# sonstigen Daten.
# Von Hand mit CURRENT_VERSION (index.html) und MyAppVersion (RankYoinker.iss)
# synchron halten - bei jedem Release alle drei zusammen hochzaehlen.
APP_VERSION = "2.0.10"
HEARTBEAT_URL = "https://rankyoinker.de/api/heartbeat"
HEARTBEAT_INTERVAL = 60
_CLIENT_ID_PATH = os.path.join(BASE, ".rankyoinker_client_id")
_HEARTBEAT_ERR_PATH = os.path.join(BASE, "rankyoinker-heartbeat-error.txt")

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


def _get_client_id():
    try:
        with open(_CLIENT_ID_PATH, "r", encoding="utf-8") as f:
            existing = f.read().strip()
            if existing:
                return existing
    except OSError:
        pass
    new_id = secrets.token_hex(16)
    try:
        with open(_CLIENT_ID_PATH, "w", encoding="utf-8") as f:
            f.write(new_id)
    except OSError:
        pass
    return new_id


def _heartbeat_lang():
    """Wie rpc.py._current_lang(), aber eigenstaendig - kein Cross-Import
    zwischen vry_log_server.py und lib/src/rpc.py noetig fuer ein Feld."""
    try:
        data = _load_json(CONFIG_JSON_FILE)
        lang = str((data or {}).get("lang", "de")).lower()
    except Exception:
        lang = "de"
    return lang if lang in RPC_LANGS else "de"


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
_UPDATE_LOG_PATH = os.path.join(BASE, "rankyoinker-update-log.txt")


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


def trigger_self_update(download_url, expected_sha256):
    _ulog(f"Update ausgeloest. downloadUrl={download_url!r} sha256={expected_sha256!r}")

    if not download_url or not expected_sha256:
        _ulog("Abgebrochen: Download-URL oder Hash fehlt.")
        return {"ok": False, "error": "Download-URL oder Hash fehlt."}

    tmp_path = os.path.join(BASE, "rankyoinker-update.exe")

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
    waiter_path = os.path.join(BASE, "rankyoinker-update-wait.bat")
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
    pythonw_path = os.path.join(BASE, "pythonw.exe")
    script_path = os.path.join(BASE, "vry_log_server.py")
    status_check = (
        'powershell -NoProfile -Command "try { $r = Invoke-WebRequest '
        "'http://127.0.0.1:1101/status' -UseBasicParsing -TimeoutSec 3; "
        'if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"'
    )
    waiter_script = (
        "@echo off\r\n"
        f'echo [%date% %time%] Update gestartet, warte auf PID {my_pid}... >> "{log_path}"\r\n'
        f"for /l %%i in (1,1,30) do (\r\n"
        f'  tasklist /fi "PID eq {my_pid}" 2^>nul ^| find /I "pythonw.exe" >nul\r\n'
        "  if errorlevel 1 goto proceed\r\n"
        "  timeout /t 1 /nobreak >nul\r\n"
        ")\r\n"
        ":proceed\r\n"
        f'echo [%date% %time%] Alter Prozess beendet (oder Zeitlimit erreicht), starte Installer ({tmp_path})... >> "{log_path}"\r\n'
        f'start "" "{tmp_path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-\r\n'
        f"for /l %%i in (1,1,110) do (\r\n"
        f"  {status_check}\r\n"
        "  if not errorlevel 1 goto installer_done\r\n"
        "  timeout /t 1 /nobreak >nul\r\n"
        ")\r\n"
        ":installer_done\r\n"
        f'echo [%date% %time%] Installations-Warteschleife beendet (laeuft oder Zeitlimit erreicht). >> "{log_path}"\r\n'
        f"{status_check}\r\n"
        "if errorlevel 1 (\r\n"
        f'  echo [%date% %time%] Log-Server laeuft nicht, starte Fallback direkt... >> "{log_path}"\r\n'
        f'  if exist "{pythonw_path}" if exist "{script_path}" start "" "{pythonw_path}" "{script_path}"\r\n'
        ")\r\n"
        'del "%~f0"\r\n'
    )

    try:
        with open(waiter_path, "w", encoding="utf-8") as f:
            f.write(waiter_script)
        subprocess.Popen(
            ["cmd", "/c", waiter_path],
            creationflags=CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
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


def _http(url, headers=None, method="GET", body=None, timeout=12):
    req = urllib.request.Request(url, method=method,
                                 data=(body.encode("utf-8") if body else None))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
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
                            {"Authorization": "Basic " + basic})
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
            },
        }
        _auth["data"] = data
        _auth["ts"] = time.time()
    note_account(data["puuid"])
    return data


def riot_get(base_key, path):
    auth = get_auth()
    status, j = _http(auth[base_key] + path, auth["headers"])
    if status in (400, 401, 403):
        auth = get_auth(force=True)          # Token abgelaufen -> einmal erneuern
        status, j = _http(auth[base_key] + path, auth["headers"])
    return status, j


def riot_post(base_key, path, body="{}"):
    return _riot_write("POST", base_key, path, body)


def riot_put(base_key, path, body="{}"):
    return _riot_write("PUT", base_key, path, body)


def _riot_write(method, base_key, path, body):
    auth = get_auth()
    status, j = _http(auth[base_key] + path, auth["headers"], method=method, body=body)
    if status in (400, 401, 403):
        auth = get_auth(force=True)
        status, j = _http(auth[base_key] + path, auth["headers"], method=method, body=body)
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
_ent_cache = {"ts": 0.0, "data": None}


def entitlements():
    """Was der Account besitzt — Grundlage fuer die Skin-Auswahl."""
    if _ent_cache["data"] and time.time() - _ent_cache["ts"] < 600:
        return _ent_cache["data"]
    auth = get_auth()
    out = {}
    for key, type_id in ITEM_TYPES.items():
        status, j = riot_get("pd", "/store/v1/entitlements/%s/%s" % (auth["puuid"], type_id))
        out[key] = [e.get("ItemID") for e in ((j or {}).get("Entitlements") or [])
                    if e.get("ItemID")] if status == 200 else []
    out["ok"] = True
    _ent_cache["data"] = out
    _ent_cache["ts"] = time.time()
    return out


def owned_agents():
    """UUIDs der freigeschalteten Agenten — zum Ausgrauen in der Auswahl."""
    auth = get_auth()
    status, j = riot_get("pd", "/store/v1/entitlements/%s/%s"
                         % (auth["puuid"], AGENT_ITEM_TYPE))
    if status != 200 or not j:
        return []
    return [e.get("ItemID") for e in (j.get("Entitlements") or []) if e.get("ItemID")]


def names_for(puuids):
    """puuid -> 'Name#Tag' (eine Anfrage fuer alle)."""
    ids = [p for p in puuids if p]
    if not ids:
        return {}
    status, j = riot_put("pd", "/name-service/v2/players", json.dumps(ids))
    out = {}
    if status == 200 and isinstance(j, list):
        for e in j:
            out[e.get("Subject")] = "%s#%s" % (e.get("GameName"), e.get("TagLine"))
    return out


# ============================ Match-Statistiken ============================

_match_cache = {}        # match_id -> match json
_stats_cache = {}        # puuid -> (timestamp, result)
_cache_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=3)   # Riot mag keine Anfrage-Lawine
STATS_TTL = 300


def _match_details(match_id):
    with _cache_lock:
        if match_id in _match_cache:
            return _match_cache[match_id]
    status, j = riot_get("pd", "/match-details/v1/matches/" + match_id)
    if status != 200 or not j:
        return None
    with _cache_lock:
        if len(_match_cache) > 60:
            _match_cache.clear()
        _match_cache[match_id] = j
    return j


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
    won = None
    for t in match.get("teams") or []:
        if t.get("teamId") == me.get("teamId"):
            won = bool(t.get("won"))
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


def player_stats(puuid, count=1):
    """Letzte `count` Matches eines Spielers, plus Summe darueber."""
    key = "%s:%d" % (puuid, count)
    now = time.time()
    with _cache_lock:
        hit = _stats_cache.get(key)
        if hit and now - hit[0] < STATS_TTL:
            return hit[1]

    out = {"puuid": puuid, "matches": [], "totals": None}
    try:
        for mid in _recent_match_ids(puuid, count):
            m = _match_details(mid)
            if not m:
                continue
            row = _extract(m, puuid)
            if row and row.get("queue") == "competitive":
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
        out["totals"] = {
            "games": len(out["matches"]),
            "kills": s("kills"), "deaths": s("deaths"), "assists": s("assists"),
            "rounds": rounds,
            "kd": round(s("kills") / (s("deaths") or 1), 2),
            "kda": round((s("kills") + s("assists")) / (s("deaths") or 1), 2),
            "hsPercent": round(s("headshots") / shots * 100, 1) if shots else None,
            "adr": round(s("damage") / rounds, 1) if rounds else None,
            "acs": round(s("score") / rounds, 1) if rounds else None,
            "wins": wins, "losses": len(out["matches"]) - wins,
            "firstKills": s("firstKills"), "multiKills": s("multiKills"),
            "plants": s("plants"), "defuses": s("defuses"),
        }

    with _cache_lock:
        _stats_cache[key] = (now, out)
    return out


_recent_cache = {}


def recent_results(puuid, count=5):
    """Sieg/Niederlage der letzten Matches — fuer die Punktreihe auf der Karte.

    Aus competitiveupdates ableitbar (Vorzeichen des RR-Gewinns), das kostet nur
    EINE Anfrage pro Spieler statt einer pro Match. Ohne Competitive-Historie
    fallen wir auf die allgemeine Match-Historie zurueck (teurer, daher weniger).
    """
    key = "r:%s:%d" % (puuid, count)
    now = time.time()
    with _cache_lock:
        hit = _recent_cache.get(key)
        if hit and now - hit[0] < STATS_TTL:
            return hit[1]

    res = []
    try:
        status, j = riot_get(
            "pd", "/mmr/v1/players/%s/competitiveupdates?startIndex=0&endIndex=%d&queue=competitive"
            % (puuid, count))
        if status == 200 and j:
            for m in (j.get("Matches") or []):
                rr = m.get("RankedRatingEarned")
                if rr is None or not m.get("MatchID"):
                    continue
                res.append({"won": rr > 0, "rr": rr, "map": m.get("MapID"), "ranked": True})
        if not res:
            for mid in _recent_match_ids(puuid, min(count, 3)):
                det = _match_details(mid)
                if not det:
                    continue
                row = _extract(det, puuid)
                if row:
                    res.append({"won": row.get("won"), "rr": None,
                                "map": row.get("map"), "ranked": False})
    except Exception:
        pass

    res = res[:count]
    with _cache_lock:
        _recent_cache[key] = (now, res)
    return res


def card_data(puuid):
    """Was die Spielerkarte braucht: Punktreihe + letztes Match fuer HS/KD-Luecken."""
    st = player_stats(puuid, 1)
    return {"puuid": puuid,
            "results": recent_results(puuid, 5),
            "last": (st.get("matches") or [None])[0]}


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
_seasons = {"ts": 0.0, "map": {}}
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
    except Exception:
        pass


def mmr_summary(puuid):
    """Aktueller Rang + RR + hoechster je erreichter Rang eines Spielers."""
    now = time.time()
    with _cache_lock:
        hit = _mmr_cache.get(puuid)
        if hit and now - hit[0] < MMR_TTL:
            return hit[1]

    out = {"rank": None, "rr": None, "peakRank": None, "peakRankAct": None}
    try:
        status, j = riot_get("pd", "/mmr/v1/players/%s" % puuid)
    except Exception:
        return out                   # nicht cachen, beim naechsten Mal erneut
    if status == 200 and j:
        last = j.get("LatestCompetitiveUpdate") or {}
        if last.get("TierAfterUpdate") is not None:
            out["rank"] = last.get("TierAfterUpdate")
            out["rr"] = last.get("RankedRatingAfterUpdate")
        seasons = (((j.get("QueueSkills") or {}).get("competitive") or {})
                   .get("SeasonalInfoBySeasonID") or {})
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
    with _cache_lock:
        if len(_mmr_cache) > 60:
            _mmr_cache.clear()
        _mmr_cache[puuid] = (now, out)
    return out


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
                        peakRankAct=r.get("peakRankAct")))
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
        _pregame_cache["ts"] = 0.0   # eigene Wahl sofort sichtbar machen
        return True, ""
    if status == 400:
        return False, ("Agent nicht waehlbar (gesperrt, nicht freigeschaltet "
                       "oder Auswahl vorbei).")
    return False, "Riot lehnte ab (Status %s)." % status


def agent_action(kind, agent_id):
    """kind: 'select' (nur hovern) oder 'lock' (fest waehlen)."""
    if not agent_id:
        return {"ok": False, "error": "Kein Agent angegeben."}
    mid = pregame_match_id()
    if not mid:
        return {"ok": False, "error": "Gerade keine Agentenauswahl aktiv."}
    ok, err = pick_agent(mid, kind, agent_id)
    return {"ok": True} if ok else {"ok": False, "error": err}


def get_loadout():
    auth = get_auth()
    status, j = riot_get("pd", "/personalization/v3/players/%s/playerloadout" % auth["puuid"])
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
    j["ok"] = True
    return j


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
        return {"ok": True}
    if status == 400:
        return {"ok": False, "error": "Riot hat das Loadout abgelehnt — im laufenden Match "
                                      "lassen sich Skins nicht wechseln."}
    return {"ok": False, "error": "Riot lehnte ab (Status %s)." % status}


PRESETS_FILE = os.path.join(BASE, "vry_presets.json")
ENCOUNTERS_FILE = os.path.join(BASE, "vry_encounters.json")
CONFIG_JSON_FILE = os.path.join(BASE, "config.json")
_presets_lock = threading.Lock()
_store_lock = threading.Lock()
_config_lock = threading.Lock()

RPC_LANGS = ("de", "en", "pl", "fr", "es", "tr")


def set_rpc_lang(lang):
    """Schreibt die App-Sprache in config.json, damit vry.exe (Discord Rich
    Presence, siehe lib/src/rpc.py) sie unabhaengig vom Browser mitliest."""
    lang = str(lang or "").lower()
    if lang not in RPC_LANGS:
        return {"ok": False, "error": "Unbekannte Sprache."}
    with _config_lock:
        data = _load_json(CONFIG_JSON_FILE)
        data["lang"] = lang
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
                     "name": names.get(p["Subject"])}
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
        res = encounters_record(mid, roster["players"],
                                {"map": roster["map"], "queue": roster["queue"]})
    except Exception:
        return False
    _enc_seen["match"] = mid
    # Stand schon in der Datei (z. B. Dienst neu gestartet) -> nichts gezählt
    return bool(res.get("ok")) and not res.get("skipped")


# ---------------------------- Party-Erkennung ----------------------------
# vry.exe färbt nur die Partys ein, die es in den Riot-Presences sieht — und
# die gibt es ausschliesslich für Freunde. Alle anderen Spieler bekommen von
# vry.exe partyNumber 0, obwohl auch dort Duos und Trios sitzen. Riot selbst
# verrät live nichts über fremde Partys: /parties/v1/players/<fremde puuid>
# antwortet mit 403 NONSELF_OPERATION, und die Match-Details des laufenden
# Matches gibt es erst nach dem Abpfiff (404).
#
# Was es aber gibt: In den ABGESCHLOSSENEN Matches steht je Spieler eine
# partyId. Wer in einem der letzten Matches dieselbe partyId hatte und heute
# wieder im selben Team steht, ist mit sehr hoher Wahrscheinlichkeit auch jetzt
# zusammen unterwegs. Genau daraus bauen wir die Gruppen — die eigene Party
# kommt zusätzlich direkt von Riot und steht damit ohne Historie fest.
#
# Grenzen (bewusst): Ein Duo, das gerade zum allerersten Mal miteinander spielt,
# hat noch keine gemeinsame Historie und bleibt unerkannt. Umgekehrt kann ein
# Paar, das früher mal zusammen gespielt hat und heute zufällig im selben Team
# landet, fälschlich als Party gelten — das ist selten, aber möglich.

_premade_cache = {"match": None, "ts": 0.0, "data": None}
PREMADE_TTL = 900          # ein Kader aendert sich waehrend eines Matches nicht
PREMADE_HISTORY = 10       # so viele vergangene Matches je Spieler
PREMADE_DETAILS = 12       # Obergrenze der Match-Details-Abfragen


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
            roster = [{"puuid": p["Subject"], "team": "ally"} for p in raw if p.get("Subject")]
            if roster:
                return mid, "PREGAME", roster
    return None, "MENUS", []


def _own_party_members():
    """Die eigene Party direkt von Riot — dafuer braucht es keine Historie."""
    try:
        pid = _party_id()
        if not pid:
            return []
        status, party = riot_get("glz", "/parties/v1/parties/%s" % pid)
        if status != 200 or not party:
            return []
        return [m.get("Subject") for m in (party.get("Members") or []) if m.get("Subject")]
    except Exception:
        return []


def _premade_history(puuid):
    """Letzte Match-IDs eines Spielers — ueber alle Modi, nicht nur Competitive.

    competitiveupdates funktioniert im Gegensatz zu /parties auch fuer fremde
    puuids; ohne queue-Filter kommen auch Unrated-Matches mit.
    """
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
    parent = {p: p for p in puuids}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 1) Die eigene Party steht fest — ganz ohne Historie
    mine = [p for p in _own_party_members() if p in parent]
    for other in mine[1:]:
        union(mine[0], other)
    sure = set(mine) if len(mine) > 1 else set()

    # 2) Alle anderen über die gemeinsame Historie
    hist = dict(zip(puuids, _pool.map(_premade_history, puuids)))
    seen, freshness = {}, {}
    for pu, ids in hist.items():
        for i, m in enumerate(ids):
            if m == mid:
                continue                      # das laufende Match zaehlt nicht
            seen.setdefault(m, set()).add(pu)
            freshness[m] = min(freshness.get(m, 99), i)

    shared = sorted((m for m, who in seen.items() if len(who) > 1),
                    key=lambda m: (freshness[m], m))[:PREMADE_DETAILS]

    proof = {}                                # puuid -> Zahl der Belege
    for m, details in zip(shared, _pool.map(_premade_details, shared)):
        if not details:
            continue
        by_party = {}
        party_of = {p.get("subject"): p.get("partyId") for p in (details.get("players") or [])}
        for pu in seen[m]:
            key = party_of.get(pu)
            if key:
                by_party.setdefault(key, []).append(pu)
        for members in by_party.values():
            for i in range(len(members)):
                for k in range(i + 1, len(members)):
                    a, b = members[i], members[k]
                    if team.get(a) != team.get(b):
                        continue              # verschiedene Teams -> heute keine Party
                    union(a, b)
                    proof[a] = proof.get(a, 0) + 1
                    proof[b] = proof.get(b, 0) + 1

    # 3) Gruppen durchnummerieren: die eigene Party zuerst, dann nach Kaderplatz
    buckets = {}
    for pu in puuids:
        buckets.setdefault(find(pu), []).append(pu)
    me = get_auth()["puuid"]
    groups = [sorted(g, key=lambda x: seat[x]) for g in buckets.values() if len(g) > 1]
    groups.sort(key=lambda g: (0 if me in g else 1, seat[g[0]]))

    parties, out_groups = {}, []
    for number, members in enumerate(groups, 1):
        live = bool(sure) and set(members) <= sure
        for pu in members:
            parties[pu] = number
        out_groups.append({
            "party": number,
            "members": members,
            "team": team.get(members[0]) or "",
            # live  = direkt von Riot (eigene Party), steht fest
            # history = aus der gemeinsamen Match-Historie abgeleitet
            "source": "live" if live else "history",
            "proof": max(proof.get(pu, 0) for pu in members),
        })

    return {"ok": True, "matchId": mid, "state": state, "parties": parties,
            "groups": out_groups, "checked": len(shared), "at": int(time.time())}


def premades():
    """Wer spielt hier mit wem zusammen? — auch bei fremden Spielern."""
    mid, state, roster = _lobby_roster()
    if not roster:
        return {"ok": False, "error": "Gerade kein laufendes Match."}
    now = time.time()
    with _cache_lock:
        hit = _premade_cache
        if hit["match"] == mid and hit["data"] and now - hit["ts"] < PREMADE_TTL:
            return hit["data"]
    data = _premades_build(mid, state, roster)
    with _cache_lock:
        _premade_cache.update({"match": mid, "ts": time.time(), "data": data})
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


def rr_history(puuid, count=15):
    """RR-Verlauf fuer die Kurve im Spieler-Fenster.

    Gecacht und mit einem zweiten Versuch abgesichert: Riot antwortet auf diesen
    Endpunkt sporadisch mit 4xx, und ein leeres Ergebnis liess die Kurve frueher
    mal erscheinen und mal nicht.
    """
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
        if len(_rr_cache) > 60:
            _rr_cache.clear()
        _rr_cache[key] = (now, out)
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


_shop_cache = {"ts": 0.0, "data": None}
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
    cached, ts = _shop_cache["data"], _shop_cache["ts"]
    if not force and cached:
        age = now - ts
        if not _shop_expired(cached, age):
            return _shop_aged(cached, age)

    fresh = _fetch_shop()
    if fresh.get("ok"):
        _shop_cache["data"] = fresh
        _shop_cache["ts"] = now
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
    _pregame_mid["id"] = j["MatchID"]
    _pregame_mid["ts"] = time.time()
    return _pregame_mid["id"]


_pregame_cache = {"ts": 0.0, "data": None}
PREGAME_TTL = 0.7   # Sekunden: die Seite darf oefter fragen als wir Riot fragen


def pregame_state():
    """Wer hat welchen Agenten gewaehlt bzw. schwebt nur darueber?

    Kurz gecacht, damit ein schnelles Polling im Browser nicht 1:1 auf Riot
    durchschlaegt — mehrere Abfragen pro Sekunde teilen sich eine Antwort.
    """
    now = time.time()
    if _pregame_cache["data"] and now - _pregame_cache["ts"] < PREGAME_TTL:
        return _pregame_cache["data"]
    result = _pregame_fetch()
    _pregame_cache["data"] = result
    _pregame_cache["ts"] = now
    return result


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
    name_map = names_for(ally_puuids)
    players = []
    for p in raw:
        sel = p.get("CharacterSelectionState") or ""
        ident = p.get("PlayerIdentity") or {}
        players.append({
            "puuid": p.get("Subject"),
            "name": name_map.get(p.get("Subject")) or None,
            "level": ident.get("AccountLevel"),
            "card": ident.get("PlayerCardID"),
            "agent": p.get("CharacterID") or None,
            # "" = nichts, "selected" = hovert nur, "locked" = fest gewählt
            "locked": sel == "locked",
            "hovering": sel == "selected",
        })

    enemies = _pregame_enemies_best_effort(m, ally_puuids)
    return {"ok": True, "matchId": mid, "phase": m.get("Phase"),
            "timeLeft": m.get("PhaseTimeRemainingNS"), "players": players,
            "enemies": enemies}


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

        name_map = names_for(candidates)
        enemy_puuids = [u for u in candidates if name_map.get(u)][:5]
        if not enemy_puuids:
            return []

        ranks = dict(zip(enemy_puuids, _pool.map(mmr_summary, enemy_puuids)))
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

_il_lock = threading.Lock()
_il = {
    "on": False,          # scharf?
    "agent": None,        # Agenten-UUID
    "name": "",           # Anzeigename (nur fuer Meldungen)
    "delay": 7.0,         # Wunsch-Verzoegerung in Sekunden
    "jitter": 1.0,        # zufaellige Streuung +/- Sekunden
    "match": None,        # Agentenauswahl, auf die gefeuert wird
    "fireAt": 0.0,        # Zeitpunkt des Sperrens
    "planned": 0.0,       # tatsaechlich gewuerfelte Verzoegerung
    "status": "off",      # off | armed | waiting | locked | done | error
    "message": "",
    "at": 0.0,            # wann zuletzt gefeuert wurde
    "lockedAt": 0.0,       # wann zuletzt gesperrt wurde (fuer den Timeout in Phase 3)
    "gen": 0,             # zaehlt jede Zustandsaenderung (fuer die Oberflaeche)
}
_il_poll = {"ts": 0.0}

# Nach dem Sperren ist noch nicht sicher, dass daraus ein echtes Spiel wird —
# wird das Match gedodged, landet man in einer neuen Agentenauswahl. Darum
# bleibt der Instalock nach dem Sperren scharf und feuert im Dodge-Fall erneut.
# Sicherheitsnetz, falls das echte Spiel nie erkannt wird (API-Aussetzer o.ä.):
IL_LOCKED_TIMEOUT = 180.0

# Der Instalock lag bisher nur im Arbeitsspeicher: Wird der Dienst beendet
# (Absturz, Update, Neustart), war das Scharfschalten still weg — man merkt es
# erst, wenn in der Agentenauswahl nichts passiert. Darum liegt der Stand jetzt
# auf der Platte und wird beim Start zurückgeholt.
INSTALOCK_FILE = os.path.join(BASE, "vry_instalock.json")
IL_RESUME_MAX = 600      # Sekunden: aelteres Scharfschalten NICHT wiederbeleben


def _il_save():
    """Aktuellen Stand sichern (ohne Sperre aufzurufen — Aufrufer haelt sie)."""
    try:
        _save_json(INSTALOCK_FILE, {
            "on": bool(_il["on"]), "agent": _il["agent"], "name": _il["name"],
            "delay": _il["delay"], "jitter": _il["jitter"], "savedAt": time.time(),
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
    fresh = (time.time() - float(data.get("savedAt") or 0)) < IL_RESUME_MAX
    resume = bool(data.get("on")) and fresh
    with _il_lock:
        _il.update({
            "agent": data.get("agent"), "name": data.get("name") or "",
            "delay": float(data.get("delay") or 7.0),
            "jitter": float(data.get("jitter") or 1.0),
            "on": resume,
            "status": "armed" if resume else "off",
            "message": ("Dienst neu gestartet — Instalock wieder scharf."
                        if resume else ""),
            "match": None, "fireAt": 0.0, "planned": 0.0,
        })


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
    _pregame_mid["id"] = j["MatchID"]
    _pregame_mid["ts"] = time.time()
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
            "gen": _il["gen"],
        }


def _il_num(body, key, default, lo, hi):
    try:
        return max(lo, min(hi, float((body or {}).get(key, default))))
    except (TypeError, ValueError):
        return default


def instalock_arm(body):
    """Scharf schalten. Die Seite darf das jederzeit erneut schicken —
    zuletzt gewinnt, ohne dass eine laufende Verzoegerung durcheinanderkommt."""
    agent = (body or {}).get("agent")
    if not agent:
        return {"ok": False, "error": "Kein Agent angegeben."}
    with _il_lock:
        _il.update({
            "on": True, "agent": agent, "name": (body or {}).get("name") or "",
            "delay": _il_num(body, "delay", 7.0, 0.0, 60.0),
            "jitter": _il_num(body, "jitter", 1.0, 0.0, 5.0),
            "match": None, "fireAt": 0.0, "planned": 0.0, "status": "armed",
            "lockedAt": 0.0,
            "message": "Wartet auf die naechste Agentenauswahl.",
            "gen": _il["gen"] + 1,
        })
        _il_save()
    _il_poll["ts"] = 0.0        # sofort nachsehen, nicht erst im naechsten Takt
    return instalock_state()


def instalock_disarm():
    with _il_lock:
        _il.update({"on": False, "status": "off", "message": "", "match": None,
                    "fireAt": 0.0, "planned": 0.0, "gen": _il["gen"] + 1})
        _il_save()
    return instalock_state()


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
            _il_finish(True, "%s gesperrt — im Spiel." % (name or "Agent"))
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


def _instalock_watcher():
    while True:
        time.sleep(IL_TICK)
        try:
            _instalock_tick()
        except Exception as e:
            _il_finish(False, "%s: %s" % (type(e).__name__, e))


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
                            {"Authorization": "Basic " + basic}, timeout=5)
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
    _ent_cache["data"] = None
    _ent_cache["ts"] = 0.0
    with _cache_lock:
        _stats_cache.clear()
        _recent_cache.clear()
        _match_cache.clear()
        _rr_cache.clear()
    _shop_cache["data"] = None
    _shop_cache["ts"] = 0.0
    _pregame_cache["data"] = None
    _pregame_cache["ts"] = 0.0
    _pregame_mid["id"] = None
    _pregame_mid["ts"] = 0.0
    _self_name["puuid"] = None
    _self_name["name"] = None


# Meldet ueber rankyoinker.de, welcher Riot-Account (Name#Tag) gerade
# RankYoinker benutzt - rein zum Nachverfolgen bei Support-Anfragen ("wer war
# das gerade, bei dem etwas nicht ging"), keine sonstigen Daten.
# Bewusst KEIN Discord-Webhook mehr direkt hier verdrahtet: diese Datei geht
# als Klartext an jede Installation raus, ein fest eingebauter Webhook waere
# fuer jeden Nutzer auslesbar und missbrauchbar gewesen. Der eigentliche
# Webhook lebt jetzt serverseitig in rankyoinker.de/.env, dieser Endpunkt
# leitet nur einen festen Nachrichtentext weiter (siehe /api/notify-login in
# server.js + sendAccountLoginNotice() in discord.js).
ACCOUNT_LOGIN_NOTIFY_URL = "https://rankyoinker.de/api/notify-login"

# _account (unten) ist reiner Prozessspeicher und wird bei JEDEM Neustart des
# Log-Servers (Updates, Abstuerze, Neuinstallationen - kommt oft vor) wieder
# auf puuid=None zurueckgesetzt. Ohne diese Datei sah darum jeder Neustart
# wie ein "neuer Account" aus, obwohl es laengst dieselbe, schon bekannte
# puuid war - das Ergebnis war Spam mit staendig denselben Accounts. Diese
# Datei haelt fest, wer schon EINMAL gemeldet wurde, ueberlebt also Neustarts.
NOTIFIED_ACCOUNTS_FILE = os.path.join(BASE, ".rankyoinker_notified_accounts.json")
_notified_lock = threading.Lock()


def _already_notified(puuid):
    with _notified_lock:
        data = _load_json(NOTIFIED_ACCOUNTS_FILE)
        seen = data.get("puuids") if isinstance(data, dict) else None
        return isinstance(seen, list) and puuid in seen


def _mark_notified(puuid):
    with _notified_lock:
        data = _load_json(NOTIFIED_ACCOUNTS_FILE)
        if not isinstance(data, dict):
            data = {}
        seen = data.get("puuids")
        if not isinstance(seen, list):
            seen = []
        if puuid not in seen:
            seen.append(puuid)
        data["puuids"] = seen
        _save_json(NOTIFIED_ACCOUNTS_FILE, data)


def _notify_account_login(puuid):
    """Loest bei einem neuen/anderen Account eine Discord-Meldung mit dem
    Riot-Tag aus. Laeuft komplett im Hintergrund (eigener Thread), damit ein
    langsamer Netzwerk-Call nicht get_auth() verzoegert, das hierher fuehrt."""
    def _send():
        try:
            tag = (names_for([puuid]) or {}).get(puuid) or puuid
            body = json.dumps({"puuid": puuid, "tag": tag}).encode("utf-8")
            req = urllib.request.Request(
                ACCOUNT_LOGIN_NOTIFY_URL,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "RankYoinker-AccountNotify"},
                method="POST",
            )
            _urlopen_public(req, timeout=10).close()
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


def note_account(puuid):
    """Von get_auth() aufgerufen: gesehene puuid festhalten (und Wechsel melden)."""
    if not puuid:
        return
    if _account["puuid"] != puuid:
        if _account["puuid"] is not None:
            _account["gen"] += 1
        _account["puuid"] = puuid
        _account["name"] = None
        if not _already_notified(puuid):
            _notify_account_login(puuid)
            _mark_notified(puuid)
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
        _pregame_mid["id"] = j["MatchID"]
        _pregame_mid["ts"] = time.time()
        return "PREGAME", j["MatchID"]
    return "MENUS", None


def game_state():
    """Spielzustand fuer die Oberflaeche — unabhaengig davon, was vry.exe sieht."""
    return {"state": _game["state"], "matchId": _game["matchId"],
            "ready": game_ready(), "at": int(_game["ts"])}


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


def _game_watcher():
    while True:
        time.sleep(GAME_INTERVAL)
        try:
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
                _game.update({"state": state, "matchId": mid, "since": now})
            _game["ts"] = now
            # Begegnungen zählen, sobald der volle Kader steht (INGAME) —
            # unabhängig davon, ob die Seite gerade offen ist
            if state == "INGAME":
                record_encounter_now(mid)
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

PAIR_FILE = os.path.join(BASE, "vry_pair.json")
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


def lcu_req(method, path, body=None, _retry=True):
    auth = lcu_auth()
    url = "https://127.0.0.1:%s%s" % (auth["port"], path)
    data = json.dumps(body) if body is not None else None
    try:
        status, j = _http(url, auth["headers"], method=method, body=data)
    except Exception:
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
        })
    return {"ok": True, "queueId": qid, "queueName": names.get(qid),
            "canStart": bool(j.get("canStartActivity", True)), "members": members}


def lol_search_state():
    try:
        status, j = lcu_get("/lol-lobby/v2/lobby/matchmaking/search-state")
    except LcuError:
        return {"ok": True, "searching": False}
    if status != 200 or not j:
        return {"ok": True, "searching": False}
    return {"ok": True, "searching": j.get("searchState") == "Searching",
            "estimatedSeconds": j.get("estimatedQueueTime"),
            "timeInQueue": j.get("timeInQueue"),
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

LOL_FILE = os.path.join(BASE, "vry_lol.json")
_lol_cfg_lock = threading.Lock()
_lol_cfg = {"autoAccept": True}


def _lol_cfg_restore():
    data = _load_json(LOL_FILE)
    with _lol_cfg_lock:
        _lol_cfg["autoAccept"] = bool(data.get("autoAccept", True))


def _lol_cfg_save():
    with _lol_cfg_lock:
        _save_json(LOL_FILE, {"autoAccept": _lol_cfg["autoAccept"]})


def lol_set_auto_accept(on):
    with _lol_cfg_lock:
        _lol_cfg["autoAccept"] = bool(on)
    _lol_cfg_save()
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


def _lol_watcher():
    while True:
        time.sleep(LOL_WATCH_INTERVAL)
        try:
            if not league_present():
                continue
            with _lol_cfg_lock:
                auto = _lol_cfg["autoAccept"]
            if auto:
                status, rc = lcu_get("/lol-matchmaking/v1/ready-check")
                if status == 200 and isinstance(rc, dict):
                    if rc.get("state") == "InProgress" and rc.get("playerResponse") == "None":
                        lcu_post("/lol-matchmaking/v1/ready-check/accept")
        except Exception:
            pass
        try:
            _lol_auto_tick()
        except Exception as e:
            # Bewusst mitschreiben statt nur zu verschlucken: ein Absturz hier
            # sah bisher von aussen exakt so aus wie "tut einfach nichts" —
            # ohne Spur war das nur noch Raten.
            _lol_auto["lastCrash"] = {"error": "%s: %s" % (type(e).__name__, e), "at": time.time()}
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


def lol_champ_select():
    try:
        status, j = lcu_get("/lol-champ-select/v1/session")
    except LcuError as e:
        return {"ok": False, "error": str(e)}
    if status != 200 or not j:
        return {"ok": False, "error": "Gerade keine Champion-Auswahl aktiv."}

    my_ids = [c.get("summonerId") for c in (j.get("myTeam") or [])]
    names = _lol_resolve_names(my_ids)
    local_cell = j.get("localPlayerCellId")

    puuids = [info.get("puuid") for info in names.values() if info.get("puuid")]
    ranks = dict(zip(puuids, _pool.map(lol_ranked, puuids))) if puuids else {}

    def cell(c, with_name):
        info = (names.get(c.get("summonerId")) or {}) if with_name else {}
        r = ranks.get(info.get("puuid")) or {}
        return {
            "cellId": c.get("cellId"), "puuid": info.get("puuid"),
            "name": info.get("name") or ("—" if with_name else None),
            "tagLine": info.get("tagLine"),
            "championId": c.get("championId") or None,
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
    for group in (j.get("actions") or []):
        for a in group:
            if (a.get("actorCellId") == local_cell and a.get("isInProgress")
                    and a.get("type") in ("pick", "ban")):
                my_action = {"id": a.get("id"), "type": a.get("type"),
                            "championId": a.get("championId") or None,
                            "completed": bool(a.get("completed"))}
            if a.get("type") == "ban" and a.get("completed") and a.get("championId"):
                banned_ids.add(a["championId"])

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
    return {"ok": True, "myTeam": my_team, "theirTeam": their_team,
            "myAction": my_action, "pickable": pickable, "bannable": bannable,
            "bannedChampionIds": sorted(banned_ids),
            "myMastery": my_mastery,
            "phase": timer.get("phase"), "timeLeft": timer.get("adjustedTimeLeftInPhase")}


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
                return {"ok": True}
            # Ältere LCU-Versionen wollen stattdessen zwei getrennte Aufrufe:
            # erst hovern, dann über einen eigenen /complete-Aufruf sperren.
            status, _ = lcu_patch(path, {"championId": champ_id})
            if status not in (200, 204):
                return {"ok": False, "error": "Riot lehnte ab (Status %s)." % status}
            status2, _ = lcu_post(path + "/complete", {"championId": champ_id})
            if status2 not in (200, 204):
                return {"ok": False, "error": "Sperren fehlgeschlagen (Status %s)." % status2}
        else:
            status, _ = lcu_patch(path, {"championId": champ_id})
            if status not in (200, 204):
                return {"ok": False, "error": "Riot lehnte ab (Status %s)." % status}
    except LcuError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


# ---- Presets: automatisches Bannen und Wählen nach Lane ----
# "Preset Ban" und "Preset Pick" - je ein Champion (plus Ausweich) je Lane,
# erkannt über assignedPosition - einmal eingestellt und dauerhaft
# gespeichert, wie "Automatisch annehmen" oben. Ausgelöst wird über denselben
# Wächter-Takt (_lol_watcher), damit auch hier ein Hintergrund-Tab nichts
# verpasst.

LOL_PRESETS_FILE = os.path.join(BASE, "vry_lol_presets.json")
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
# trotzdem sofort (siehe _lol_auto_tick) — nur das Sperren wartet.
LOL_AUTO_DELAY = 2.0
LOL_AUTO_JITTER = 1.2
# Ein Versuch je Watcher-Takt (~1x/Sekunde) statt einer schnellen Serie —
# ein einzelner abgelehnter Sperrversuch (z. B. weil die LCU kurz nach dem
# Hovern noch nicht so weit ist) darf nicht dauerhaft aufgeben. Erst nach
# LOL_AUTO_MAX_ATTEMPTS Sekunden ohne Erfolg wird's dem manuellen Klicken
# überlassen.
LOL_AUTO_MAX_ATTEMPTS = 15

_lol_auto = {"actionId": None, "hoveredChampionId": None, "fireAt": 0.0, "attempts": 0,
             "lastResult": None, "lastSkip": None}


def _lol_auto_reset():
    _lol_auto.update({"actionId": None, "hoveredChampionId": None, "fireAt": 0.0, "attempts": 0})


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
    with _lol_presets_lock:
        return {"ok": True, "autoBan": _lol_presets["autoBan"], "autoPick": _lol_presets["autoPick"],
                "ban": _lol_presets["ban"], "picks": _lol_presets["picks"],
                "lastAutoHover": _lol_auto.get("lastHover"),
                "lastAutoResult": _lol_auto.get("lastResult"),
                "lastAutoSkip": _lol_auto.get("lastSkip"),
                "lastAutoCrash": _lol_auto.get("lastCrash")}


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
    return lol_presets_state()


def _lol_priority_ids(slots):
    """Champion-IDs einer Prioritaetsliste (Hauptziel zuerst, dann die
    Ausweichchampions) — leere Slots werden uebersprungen."""
    return [s["id"] for s in (slots or []) if s and s.get("id")]


def _lol_auto_lane_candidates(entries_by_lane, lane):
    """Champion-IDs in Prioritaetsreihenfolge fuer eine Lane (Hauptziel zuerst,
    dann die Ausweichchampions) — leere Slots werden uebersprungen. Gilt
    gleichermassen fuer Baenne und Picks, beide sind {Lane: [...]} strukturiert."""
    return _lol_priority_ids(entries_by_lane.get(lane) if lane else entries_by_lane.get("OTHER"))


def _lol_auto_tick():
    """Einmal je Watcher-Takt: laufende eigene Aktion mit dem passenden Preset
    abgleichen. Sobald ein Ziel feststeht, wird SOFORT gehovert (zeigt die
    Absicht direkt an); gesperrt wird erst nach kurzer, menschlich wirkender
    Verzoegerung — und die Wahl wird bis dahin bei jedem Takt neu geprueft, ein
    Bann bricht also ab, sobald ein Team-Mitglied den Ziel-Champion hovert, und
    ein Pick wechselt automatisch auf den naechsten Ausweichchampion, falls der
    Hauptpick inzwischen weg ist."""
    with _lol_presets_lock:
        auto_ban, auto_pick = _lol_presets["autoBan"], _lol_presets["autoPick"]
        bans = {k: list(v) for k, v in _lol_presets["ban"].items()}
        picks = {k: list(v) for k, v in _lol_presets["picks"].items()}
    if not auto_ban and not auto_pick:
        return

    cs = lol_champ_select()
    if not cs.get("ok"):
        _lol_auto_reset()
        return
    action = cs.get("myAction")
    if not action or action.get("completed"):
        _lol_auto_reset()
        return

    my_team = cs.get("myTeam") or []
    their_team = cs.get("theirTeam") or []
    me = next((m for m in my_team if m.get("me")), None)
    mate_champs = {m.get("championId") for m in my_team if not m.get("me") and m.get("championId")}
    # Nur in manchen Modi überhaupt gefüllt (siehe lol_champ_select) — meist
    # bleibt das während der eigenen Draft-Phase leer, dann hat dieser Check
    # schlicht keine Wirkung.
    enemy_champs = {m.get("championId") for m in their_team if m.get("championId")}

    desired, skip_reason = None, None
    lane = (me or {}).get("position")
    if action.get("type") == "ban":
        if auto_ban:
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
            desired = next((cid for cid in candidates
                            if cid in bannable and cid not in mate_champs), None)
            if desired is None and candidates:
                skip_reason = ("Keiner der hinterlegten Bann-Champions fuer diese Lane ist gerade bannbar "
                               "(schon gebannt oder von einem Team-Mitglied gehovert).")
    elif action.get("type") == "pick" and auto_pick:
        candidates = _lol_auto_lane_candidates(picks, lane)
        pickable = set(cs.get("pickable") or [])
        avail = [cid for cid in candidates if cid in pickable]
        # Erst den Ausweich bevorzugen, der nicht schon beim Gegner hängt (kein
        # Mirror-Match); hängt der Gegner an ALLEN hinterlegten Kandidaten,
        # lieber den bestplatzierten davon nehmen als am Ende gar nichts zu picken.
        desired = next((cid for cid in avail if cid not in enemy_champs), None)
        if desired is None and avail:
            desired = avail[0]
        if desired is None and candidates:
            skip_reason = "Keiner der hinterlegten Ausweichchampions fuer diese Lane ist gerade waehlbar."

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
        # Auch hier merken statt nur abschicken — sonst sieht ein stumm
        # abgelehntes Hovern von aussen genauso aus wie gar keins.
        _lol_auto["lastHover"] = {"ok": bool(hover_result.get("ok")), "error": hover_result.get("error"),
                                   "championId": desired, "actionId": action.get("id"), "at": time.time()}
        wait = max(0.0, LOL_AUTO_DELAY + random.uniform(-LOL_AUTO_JITTER, LOL_AUTO_JITTER))
        _lol_auto.update({"actionId": action.get("id"), "hoveredChampionId": desired,
                          "fireAt": time.time() + wait, "attempts": 0})
        return
    if time.time() < _lol_auto["fireAt"] or _lol_auto["attempts"] >= LOL_AUTO_MAX_ATTEMPTS:
        return
    _lol_auto["attempts"] += 1
    # Ergebnis merken (nicht nur zurückgeben) — sonst lässt sich ein
    # wiederholt scheiterndes Sperren nur während des laufenden Matches live
    # beobachten. So bleibt der letzte Versuch bis zum nächsten auch danach
    # über /api/lol/presets sichtbar.
    result = lol_champ_action(action.get("id"), desired, True)
    _lol_auto["lastResult"] = {"ok": bool(result.get("ok")), "error": result.get("error"),
                                "at": time.time(), "championId": desired, "attempts": _lol_auto["attempts"]}


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
        status, j = _http(LIVE_URL + path, {}, timeout=4)
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
    return {"ok": True, "players": out, "gold": active.get("currentGold"),
            "level": active.get("level"), "myMastery": lol_champion_mastery(my_champ_id),
            "arena": is_arena}


# ---- Begegnungen (eigenes League-Konto) ----
# Gleiches Prinzip wie bei VALORANT oben (siehe encounters_record), aber in
# einer eigenen Datei und mit einer eigenen "Wer bin ich" — die LCU kennt kein
# get_auth()/Lockfile, die puuid kommt hier von lol_current_summoner(). Statt
# einer Karte (die es bei League nicht gibt) wird die Warteschlange notiert.

LOL_ENCOUNTERS_FILE = os.path.join(BASE, "vry_lol_encounters.json")
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
    if not game_id or _lol_enc_seen["match"] == game_id:
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
_ORIGIN_OK = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\]|vry)(:\d+)?$", re.I)


class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, *args):
        pass

    def _cors(self):
        """Freigabe nur fuer die eigene Seite. 'null' deckt den Aufruf von
        index.html per file:// ab — dort gibt es keine Cookies, der Zugriff
        laeuft dann ueber die Localhost-Regel."""
        origin = self.headers.get("Origin")
        if not origin:
            return
        if _ORIGIN_OK.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        elif origin == "null":
            self.send_header("Access-Control-Allow-Origin", "null")
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

    def _local_only(self):
        """Kopplung verwalten darf nur der PC selbst — sonst koennte sich ein
        gekoppeltes Handy weitere Geraete dazuholen."""
        if _is_local(self._client_ip()):
            return True
        self._json({"ok": False, "error": "Nur am PC selbst möglich."}, 403)
        return False

    def _authorized(self):
        if _is_local(self._client_ip()):
            return True
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
            self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def do_OPTIONS(self):
        # Der Vorab-Check des Browsers darf nicht an der Kopplung scheitern
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else {}
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
        elif path == "/api/agent/select":
            self._safe(lambda: agent_action("select", body.get("agent")))
        elif path == "/api/agent/lock":
            self._safe(lambda: agent_action("lock", body.get("agent")))
        elif path == "/api/loadout/apply":
            self._safe(lambda: put_loadout(body))
        elif path == "/api/set-lang":
            self._safe(lambda: set_rpc_lang(body.get("lang")))
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
        elif path == "/api/lol/presets/auto":
            self._safe(lambda: lol_presets_set_auto(body.get("ban"), body.get("pick")))

        elif path == "/api/rankyoinker/self-update":
            # Nur vom PC selbst - nicht über ein gekoppeltes Handy auslösbar.
            if self._local_only():
                self._safe(lambda: trigger_self_update(body.get("downloadUrl"), body.get("sha256")))
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

        elif path == "/favicon.ico":
            self._send(204, "")

        elif path == "/api/pair":
            if self._local_only():
                self._safe(pair_state)

        elif path.startswith("/log"):
            name, lines = self._read_lines()
            out = {"file": name, "log": "".join(lines[-500:]),
                   "progress": parse_progress(lines)}
            out.update(proc_state())
            self._json(out)

        elif path.startswith("/status"):
            _, lines = self._read_lines()
            # Der Instalock kommt hier bewusst mit: Startet der Dienst neu und
            # nimmt ein Scharfschalten wieder auf, merkt die Seite das sonst
            # nicht — sie fragt /api/instalock nur, solange sie es selbst kennt.
            with _il_lock:
                il = {"on": bool(_il["on"]), "gen": _il["gen"]}
            out = {"progress": parse_progress(lines), "game": game_state(),
                   "instalock": il, "activeGame": detect_active_game(),
                   "lol": lol_gamestate()}
            out.update(proc_state())
            out.update(account_state())
            self._json(out)

        elif path.startswith("/start"):
            res = start_vry()
            res.setdefault("ok", True)
            res["running"] = vry_running() or res.get("running", False)
            self._json(res)

        elif path.startswith("/shutdown"):
            # Alles beenden, auch dieser Dienst (nur über stop_vry.bat)
            self._json({"ok": True, "running": False, "shuttingDown": True})
            try:
                self.wfile.flush()
            except Exception:
                pass
            stop_everything()

        elif path.startswith("/stop"):
            res = stop_vry_only() or {}
            res.setdefault("ok", True)
            res["running"] = vry_running()
            self._json(res)

        elif path == "/api/local-config":
            # Liest ausschliesslich fuer den Update-Check gedachte, lokale
            # Einstellungen aus config.json (siehe checkForUpdate() in
            # index.html) - aktuell nur "updateChannel" ("stable"/"dev").
            # Kein Riot-Zugriff noetig, darum ganz oben und ohne try/RiotError.
            try:
                data = _load_json(CONFIG_JSON_FILE)
                channel = str((data or {}).get("updateChannel", "stable")).lower()
            except Exception:
                channel = "stable"
            self._json({"ok": True, "updateChannel": channel if channel == "dev" else "stable"})

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
            rows = list(_pool.map(card_data, puuids[:12]))
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
            self._safe(get_loadout)

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

        elif path == "/api/gamestate":
            # Was Riot sagt — unabhängig davon, was vry.exe gerade anzeigt
            self._json(dict(game_state(), ok=True))

        elif path == "/api/rrhistory":
            puuid = q.get("puuid", [""])[0]
            try:
                n = max(3, min(20, int(q.get("n", ["15"])[0])))
            except ValueError:
                n = 15
            self._safe(lambda: rr_history(puuid, n) if puuid
                       else {"ok": False, "error": "puuid fehlt."})

        elif path == "/api/player":
            puuid = q.get("puuid", [""])[0]
            try:
                count = max(1, min(50, int(q.get("n", ["5"])[0])))
            except ValueError:
                count = 5
            if not puuid:
                self._json({"ok": False, "error": "puuid fehlt"})
                return
            data = player_stats(puuid, count)
            data["ok"] = "error" not in data
            self._json(data)

        elif path == "/api/pregame":
            try:
                self._json(pregame_state())
            except RiotError as e:
                self._json({"ok": False, "error": str(e)})
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        elif path == "/api/dodge":
            try:
                self._json(dodge_pregame())
            except RiotError as e:
                self._json({"ok": False, "error": str(e)})
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

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
            self._json(lol_gamestate())

        elif path == "/api/lol/lobby":
            self._json(lol_lobby())

        elif path == "/api/lol/searchstate":
            self._json(lol_search_state())

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


def _tray_open_overlay():
    try:
        webbrowser.open("http://localhost:1101/")
    except Exception as e:
        _ulog("Tray: Browser oeffnen fehlgeschlagen: %s: %s" % (type(e).__name__, e))


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
            user32.AppendMenuW(hmenu, MF_STRING, _ID_MENU_OPEN, "Website öffnen")
            user32.AppendMenuW(hmenu, MF_STRING, _ID_MENU_QUIT, "Programm schließen")
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

    # Wacht über vry.exe: genau eine Instanz, Neustart nach einem Absturz
    threading.Thread(target=_watchdog, daemon=True).start()
    # Erkennt einen Account-Wechsel und wirft dann alle Caches weg
    threading.Thread(target=_account_watcher, daemon=True).start()
    # Verfolgt den echten Spielzustand und holt vry.exe zurück, wenn es ein
    # laufendes Match verpasst hat
    threading.Thread(target=_game_watcher, daemon=True).start()
    # Instalock: läuft hier statt im Browser, damit ein Hintergrund-Tab die
    # Verzögerung nicht verschleppt. Ein frisches Scharfschalten überlebt
    # einen Neustart des Dienstes.
    _il_restore()
    threading.Thread(target=_instalock_watcher, daemon=True).start()
    # League of Legends: automatisches Annehmen der Spielfindung, unabhängig
    # vom VALORANT-Zweig oben — gleiches Prinzip wie der Instalock.
    _lol_cfg_restore()
    _lol_presets_restore()
    threading.Thread(target=_lol_watcher, daemon=True).start()
    # RankYoinker: anonymer Aktiv-Nutzer-Ping für die Website (siehe oben)
    threading.Thread(target=_active_user_heartbeat, daemon=True).start()
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
