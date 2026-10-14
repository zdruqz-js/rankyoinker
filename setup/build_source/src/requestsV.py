import base64
import json
import threading
import time
from json.decoder import JSONDecodeError
import requests
from colr import color
import os
from requests.exceptions import ConnectionError
from src.paths import data_dir

# fetch() gab bei einem 429 (Rate-Limit) bisher UNBEGRENZT oft mit wachsender
# Wartezeit neu auf - blieb Riot laenger als ueblich rate-limitiert (kommt
# vor, siehe Diagnose-Logs: teils >2 Minuten am Stueck), blockierte das den
# aufrufenden Thread ebenso lange. Bei begrenzten ThreadPoolExecutor-Workern
# (main.py) verhungerten dadurch ALLE anderen wartenden Spieler mit, obwohl
# nur ein paar Spieler tatsaechlich betroffen waren. Ab MAX_FETCH_ATTEMPTS
# wird stattdessen aufgegeben und die (fehlgeschlagene) Antwort zurueckgegeben
# - Aufrufer (rank.py/player_stats.py) haben fuer genau diesen Fall schon
# Standardwerte/Fallbacks, zeigen fuer den betroffenen Spieler dann eben "N/A"
# statt die komplette Ladung fuer alle zu blockieren.
MAX_FETCH_ATTEMPTS = 2

# ---- Bremse fuer pd.*.a.pvp.net (gilt fuer alle Threads gemeinsam) ----
# Beim Matchstart haben vry.exe und der lokale Dienst denselben Riot-Server so
# dicht hintereinander abgefragt, dass Riot mit 429 antwortete und Cloudflare
# die IP danach kurz ganz sperrte (HTML-Seite "Access denied ... used
# Cloudflare to restrict access" statt Daten, Nutzer-Log 2026-10-13). Bisher
# wartete jeder Thread fuer sich und klopfte danach erneut an - das verlaengert
# so eine Sperre nur. Jetzt: Mindestabstand zwischen pd-Anfragen, und nach
# einem 429 bzw. einer Cloudflare-Sperre warten ALLE Threads gemeinsam.
#
# Seit 2.4.9 GEMEINSAM mit dem lokalen Dienst (vry_log_server.py, gleiche
# Logik, gleiche Datei): Vorher bremste jeder Prozess nur sich selbst, zusammen
# gingen beim Matchstart ueber 7 Anfragen/s raus, und waehrend einer
# pausierte, fragte der andere weiter. Takt, Pause und Sperrzaehler liegen in
# DATA_DIR/pd-gate.json, geschuetzt durch pd-gate.lock (per O_EXCL angelegt).
# Klappt das nicht, bremst vry.exe wieder nur sich selbst (wie vor 2.4.9).
# Pause nach einer Cloudflare-Sperre erst 20 s, dann EINE Testanfrage; ist die
# Sperre noch da, 45 s, dann 90 s. 10 Minuten ohne Sperre setzen das zurueck.
PD_MIN_INTERVAL = 0.3          # beide Prozesse zusammen hoechstens ~3 Anfragen pro Sekunde
PD_MAX_INTERVAL = 1.5
PD_RATE_PAUSE = 12.0           # nach einem normalen 429 von Riot
PD_BLOCK_PAUSES = (20.0, 45.0, 90.0)   # nach der 1./2./3. Cloudflare-Sperre in Folge
PD_STRIKE_RESET = 600.0
PD_PROBE_TIMEOUT = 15.0
PD_GATE_FILE = os.path.join(data_dir(), "pd-gate.json")
PD_GATE_LOCKFILE = os.path.join(data_dir(), "pd-gate.lock")
_pd_gate = {"next": 0.0, "interval": PD_MIN_INTERVAL, "blocked_until": 0.0, "reason": "",
            "strikes": 0, "last_block": 0.0, "need_probe": False, "probe_until": 0.0}
_pd_lock = threading.Lock()


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
        _pd_lock.acquire()
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
            _pd_lock.release()
        return False


def pd_wait():
    """Wartet auf den naechsten freien Platz fuer eine pd-Anfrage, auch ueber
    eine Sperre hinweg. Rueckgabe "probe" = die eine Testanfrage nach einer
    Pause, sonst True - immer an pd_feedback() zurueckgeben."""
    slot = None
    while True:
        with _PdState() as g:
            now = time.time()
            if now < g["blocked_until"]:
                slot = None                     # reservierter Platz verfaellt mit der Sperre
                sleep_for = min(g["blocked_until"] - now, 1.0)
            elif now < g["probe_until"]:
                sleep_for = 0.2                 # Testanfrage laeuft gerade
            elif g["need_probe"]:
                g["need_probe"] = False
                g["probe_until"] = now + PD_PROBE_TIMEOUT
                g["next"] = max(g["next"], now + g["interval"])
                return "probe"
            elif slot is not None and slot <= now:
                return True
            else:
                if slot is None:
                    slot = max(now, g["next"])
                    g["next"] = slot + g["interval"]
                    if slot <= now:
                        return True             # Platz ist sofort frei
                sleep_for = max(0.0, slot - now)
        time.sleep(sleep_for)


def is_cloudflare_block(response):
    """Cloudflare-Sperre statt Riot-Antwort: HTML-Seite ("Access denied") oder
    Klartext "error code: 1015" (Cloudflares Ratenlimit - bis 2.4.9 hier als
    normales 429 behandelt, also nur 10 s Pause bei laufender Sperre)."""
    if response is None or response.status_code not in (403, 429):
        return False
    head = (response.text or "")[:2000].lower()
    return ("cloudflare" in head or "access denied" in head or "<html" in head
            or "error code:" in head)


def short_error(response):
    """Kurze Fehlerbeschreibung fuers Log - nie die komplette HTML-Seite."""
    if is_cloudflare_block(response):
        return "Cloudflare-Sperre (zu viele Anfragen an Riot) - kurze Pause"
    text = (response.text or "").replace("\n", " ").strip()
    return text[:300] + (" …" if len(text) > 300 else "")


def pd_feedback(response, ticket, log):
    """Ergebnis einer pd-Anfrage zurueckmelden (response=None bei Verbindungsfehler):
    Sperre setzen, Tempo anpassen, Testanfrage abschliessen."""
    status = response.status_code if response is not None else None
    probe = ticket == "probe"
    msg = None
    with _PdState() as g:
        now = time.time()
        if probe:
            g["probe_until"] = 0.0
        if is_cloudflare_block(response):
            # Mehrere gleichzeitig laufende Anfragen melden dieselbe Sperre - nur die erste zaehlt.
            if now >= g["blocked_until"]:
                strikes = g["strikes"] + 1 if now - g["last_block"] < PD_STRIKE_RESET else 1
                pause = PD_BLOCK_PAUSES[min(strikes, len(PD_BLOCK_PAUSES)) - 1]
                g.update(strikes=strikes, last_block=now, blocked_until=now + pause,
                         need_probe=True, interval=PD_MAX_INTERVAL, reason="cloudflare")
                msg = f"pd: {short_error(response)} - alle pd-Anfragen pausieren {int(pause)} s ({strikes}. in Folge)"
        elif status == 429:
            try:
                wait = float(response.headers.get("Retry-After") or 0)
            except (TypeError, ValueError, AttributeError):
                wait = 0
            pause = max(PD_RATE_PAUSE, min(wait, 120))
            g["blocked_until"] = max(g["blocked_until"], now + pause)
            g["interval"] = min(PD_MAX_INTERVAL, g["interval"] * 1.6)
            g["reason"] = "ratelimit"
            if probe:
                g["need_probe"] = True
            msg = f"pd: Rate-Limit 429 - alle pd-Anfragen pausieren {int(pause)} s"
        elif status and status < 400:
            g["interval"] = max(PD_MIN_INTERVAL, g["interval"] * 0.97)
            if probe:
                # Diese Pause hat gereicht: die naechste Sperre faengt wieder bei ihr an
                g["strikes"] = max(0, g["strikes"] - 1)
        elif probe:
            g["need_probe"] = True              # Testanfrage ohne verwertbare Antwort -> naechste testet
    if msg and log:
        log(msg)

class Requests:
    def __init__(self, version, log, Error):
        self.Error = Error
        self.version = version
        self.headers = {}
        self.log = log


        self.lockfile = self.get_lockfile()
        self.region = self.get_region()
        self.pd_url = f"https://pd.{self.region[0]}.a.pvp.net"
        self.glz_url = f"https://glz-{self.region[1][0]}.{self.region[1][1]}.a.pvp.net"
        self.log(f"Api urls: pd_url: '{self.pd_url}', glz_url: '{self.glz_url}'")
        self.region = self.region[0]
        
        self.puuid = ''
        #fetch puuid so its avaible outside
        if not self.get_headers(init=True):
            self.log("Invalid URI format, invalid lockfile, going back to menu")
            self.get_lockfile(ignoreLockfile=True)
        

    # RankYoinker checks/installs its own updates via rankyoinker.de instead
    # (see checkForUpdate() in index.html + install_all.py) - the base
    # project's own GitHub-releases-based self-updater (which used to live
    # here, driving updatescript.bat) pointed at the wrong repo/versioning
    # for a fork and has been removed.

    @staticmethod
    def check_status():
        # checking status
        try:
            rStatus = requests.get(
                "https://raw.githubusercontent.com/zayKenyon/VALORANT-rank-yoinker/main/status.json")
        except requests.exceptions.RequestException:
            print(color("[WARNING] Unable to check status - skipping...", fore=(255, 165, 0)))
            return

        try:
            status_data = rStatus.json()
            if not status_data["status_good"] or status_data["print_message"]:
                status_color = (255, 0, 0) if not status_data["status_good"] else (0, 255, 0)
                print(color(status_data["message_to_display"], fore=status_color))
        except Exception:
            print(color("[WARNING] Failed processing status - skipping...", fore=(255, 165, 0)))
            return
            
    def fetch(self, url_type: str, endpoint: str, method: str, rate_limit_seconds=5, attempt=1, max_attempts=None):
        # max_attempts=1: kein Schlaf + zweiter Versuch bei !ok - fuer Aufrufer,
        # die ohnehin gleich wieder fragen (z. B. Party beim Spielstart)
        limit = max_attempts or MAX_FETCH_ATTEMPTS
        try:
            if url_type == "glz":
                # glz-*.*.a.pvp.net is Riot's real cloud API (public, trusted
                # cert) - verify=False here (like pd/custom below) had no
                # legitimate reason and left every request open to a trivial
                # MITM (see security report 2026-09-28). Only the genuinely
                # self-signed local client API (url_type "local" below,
                # get_headers()) skips verification.
                response = requests.request(method, self.glz_url + endpoint, headers=self.get_headers(), verify=True)
                self.log(f"fetch: url: '{url_type}', endpoint: {endpoint}, method: {method},"
                    f" response code: {response.status_code}")

                if response.status_code == 404:
                    return response.json()

                try:
                    if response.json().get("errorCode") == "BAD_CLAIMS":
                        self.log("detected bad claims")
                        self.headers = {}
                        return self.fetch(url_type, endpoint, method, max_attempts=max_attempts)
                except JSONDecodeError:
                    pass
                if not response.ok:
                    if response.status_code == 429:
                        self.log("response not ok glz endpoint: rate limit 429")
                    else:
                        self.log("response not ok glz endpoint: " + response.text)
                    if attempt >= limit:
                        if limit > 1:
                            self.log(f"glz endpoint {endpoint} weiterhin nicht ok nach {attempt} Versuchen - gebe auf")
                        return response.json()
                    time.sleep(rate_limit_seconds+5)
                    self.headers = {}
                    # War hier ohne "return": der Retry lief zwar, sein Ergebnis
                    # wurde aber verworfen, und die Funktion gab stattdessen die
                    # Fehler-Antwort von OBEN zurueck (kein "MatchID" o.ae. drin,
                    # sah fuer Aufrufer wie eine leere/negative Antwort aus). Bei
                    # /core-game/v1/players/... (glz) fuehrte ein einzelner
                    # Rate-Limit-Treffer dadurch dazu, dass der Zustandsabgleich
                    # faelschlich "nicht mehr im Match" meldete und das laufende
                    # Laden der Spielerliste mittendrin neu startete.
                    return self.fetch(url_type, endpoint, method, rate_limit_seconds=rate_limit_seconds+5, attempt=attempt+1,
                                      max_attempts=max_attempts)
                return response.json()
            elif url_type == "pd":
                # pd.*.a.pvp.net is Riot's real cloud API too - see comment above.
                ticket = pd_wait()
                try:
                    response = requests.request(method, self.pd_url + endpoint, headers=self.get_headers(), verify=True)
                except Exception:
                    pd_feedback(None, ticket, self.log)
                    raise
                pd_feedback(response, ticket, self.log)
                self.log(
                    f"fetch: url: '{url_type}', endpoint: {endpoint}, method: {method},"
                    f" response code: {response.status_code}")
                if response.status_code == 404:
                    return response

                try:
                    if response.json().get("errorCode") == "BAD_CLAIMS":
                        self.log("detected bad claims")
                        self.headers = {}
                        return self.fetch(url_type, endpoint, method, max_attempts=max_attempts)
                except JSONDecodeError:
                    pass

                if not response.ok:
                    blocked = response.status_code == 429 or is_cloudflare_block(response)
                    if not blocked:
                        # Sperren hat pd_feedback() oben schon gemeldet und fuer alle pausiert
                        self.log(f"response not ok pd endpoint, {short_error(response)}")
                    if attempt >= limit:
                        if limit > 1:
                            self.log(f"pd endpoint {endpoint} weiterhin nicht ok nach {attempt} Versuchen - gebe auf")
                        return response
                    if not blocked:
                        time.sleep(rate_limit_seconds+5)
                    self.headers = {}
                    return self.fetch(url_type, endpoint, method, rate_limit_seconds=rate_limit_seconds+5, attempt=attempt+1,
                                      max_attempts=max_attempts)
                return response
            elif url_type == "local":
                # 127.0.0.1 = the Riot Client's own local API, self-signed
                # certificate by design - verification is intentionally off
                # ONLY here and in get_headers() below, nowhere else.
                local_headers = {'Authorization': 'Basic ' + base64.b64encode(
                    ('riot:' + self.lockfile['password']).encode()).decode()}

                max_retries = 3
                for i in range(max_retries):
                    try:
                        response = requests.request(method, f"https://127.0.0.1:{self.lockfile['port']}{endpoint}",
                                                    headers=local_headers,
                                                    verify=False, timeout=5)
                        if response.status_code == 200 and response.json().get("errorCode") != "RPC_ERROR":
                            if endpoint != "/chat/v4/presences":
                                self.log(
                                    f"fetch: url: '{url_type}', endpoint: {endpoint}, method: {method},"
                                    f" response code: {response.status_code}")
                            return response.json()
                        else:
                            self.log(f"Local API is not ready yet (RPC_ERROR or status code {response.status_code}). Retrying...")
                            time.sleep(5)
                    except (requests.exceptions.RequestException, ConnectionError):
                        self.log(f"Connection error on local request. Retrying... ({i + 1}/{max_retries})")
                        time.sleep(5)
                
                self.log(f"Failed to connect to local client after {max_retries} attempts.")
                return None
            elif url_type == "custom":
                # Only ever called with a hardcoded shared.*.a.pvp.net URL
                # (see content.py) - a real, publicly-trusted endpoint, same
                # as pd/glz above.
                response = requests.request(method, f"{endpoint}", headers=self.get_headers(), verify=True)
                self.log(
                    f"fetch: url: '{url_type}', endpoint: {endpoint}, method: {method},"
                    f" response code: {response.status_code}")
                if not response.ok: self.headers = {}
                return response.json()
        except json.decoder.JSONDecodeError:
            self.log(f"JSONDecodeError in fetch function, resp.code: {response.status_code}, resp_text: '{response.text}")
            print(response)
            print(response.text)

    def get_region(self):
        path = os.path.join(os.getenv('LOCALAPPDATA'), R'VALORANT\Saved\Logs\ShooterGame.log')
        with open(path, "r", encoding="utf8") as file:
            while True:
                line = file.readline()
                if '.a.pvp.net/account-xp/v1/' in line:
                    pd_url = line.split('.a.pvp.net/account-xp/v1/')[0].split('.')[-1]
                elif 'https://glz' in line:
                    glz_url = [(line.split('https://glz-')[1].split(".")[0]),
                               (line.split('https://glz-')[1].split(".")[1])]
                if "pd_url" in locals().keys() and "glz_url" in locals().keys():
                    self.log(f"got region from logs '{[pd_url, glz_url]}'")
                    if pd_url == "pbe":
                        return ["na", "na-1", "na"]
                    return [pd_url, glz_url]

    def get_current_version(self):
        path = os.path.join(os.getenv('LOCALAPPDATA'), R'VALORANT\Saved\Logs\ShooterGame.log')
        with open(path, "r", encoding="utf8") as file:
            while True:
                line = file.readline()
                if 'CI server version:' in line:
                    version_without_shipping = line.split('CI server version: ')[1].strip()
                    version = version_without_shipping.split("-")
                    version = "-".join(version)
                    self.log(f"got version from logs '{version}'")
                    return version

    def get_lockfile(self, ignoreLockfile=False):
        #ignoring lockfile is for when lockfile exists but it's not really valid, (local endpoints are not initialized yet)
        path = os.path.join(os.getenv('LOCALAPPDATA'), R'Riot Games\Riot Client\Config\lockfile')
        
        if self.Error.LockfileError(path, ignoreLockfile=ignoreLockfile):
            with open(path) as lockfile:
                self.log("opened lockfile")
                data = lockfile.read().split(':')
                keys = ['name', 'PID', 'port', 'password', 'protocol']
                return dict(zip(keys, data))


    def get_headers(self, refresh=False, init=False):
        if self.headers == {} or refresh:
            try_again = True
            while try_again:
                local_headers = {'Authorization': 'Basic ' + base64.b64encode(
                    ('riot:' + self.lockfile['password']).encode()).decode()}
                try:
                    response = requests.get(f"https://127.0.0.1:{self.lockfile['port']}/entitlements/v1/token",
                                            headers=local_headers, verify=False)
                    # Nur die URL: local_headers enthaelt das Lockfile-Passwort
                    # (Basic riot:...), und das Log ist ueber /log auch fuer
                    # gekoppelte Handys lesbar (Hinweis 2026-10-13)
                    self.log(f"https://127.0.0.1:{self.lockfile['port']}/entitlements/v1/token")
                except ConnectionError:
                    self.log(f"https://127.0.0.1:{self.lockfile['port']}/entitlements/v1/token")
                    self.log("Connection error, retrying in 1 seconds, getting new lockfile")
                    time.sleep(1)
                    self.lockfile = self.get_lockfile()
                    continue
                entitlements = response.json()
                if entitlements.get("message") == "Entitlements token is not ready yet":
                    try_again = True
                    time.sleep(1)
                elif entitlements.get("message") == "Invalid URI format":
                    self.log(f"Invalid uri format: {entitlements}")
                    if init:
                        return False
                    else:
                        try_again = True
                        time.sleep(5)
                else:
                    try_again = False

            self.puuid = entitlements['subject']
            headers = {
                'Authorization': f"Bearer {entitlements['accessToken']}",
                'X-Riot-Entitlements-JWT': entitlements['token'],
                'X-Riot-ClientPlatform': "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjog"
                                         "IldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5"
                                         "MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9",
                'X-Riot-ClientVersion': self.get_current_version(),
                "User-Agent": "ShooterGame/13 Windows/10.0.19043.1.256.64bit"
            }
            self.headers = headers
        return self.headers
