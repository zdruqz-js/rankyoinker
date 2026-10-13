"""Plattencache fuer die statischen valorant-api.com-Kataloge.

vry.exe holte bei JEDEM Start alle Kataloge neu (Skins, Waffen, Sprays,
Buddies, Agenten, Titel, Spielerkarten, Karten - zusammen gut 10 MB) und las
die grossen davon im Match fuer jeden Spieler erneut ein (requests'
Response.json() parst bei jedem Aufruf neu). Die Daten aendern sich aber nur
mit einem Patch.

Jetzt: einmal geholt, auf Platte unter DATA_DIR/cache/valorant-api/ gemerkt
und pro Programmlauf nur einmal geparst. Ob die Daten noch stimmen, sagt
valorant-api.com selbst: /v1/version (gut 200 Byte, einmal pro Start) nennt
den Datenstand der API. Gleicher Stand -> Cache (hoechstens API_MAX_AGE),
neuer Stand -> neu holen. Die Spielversion taugt dafuer nicht: nach einem
Patch zieht valorant-api.com oft erst Stunden spaeter nach (Hinweis aus der
Session "Ladezeit der letzten 5 Matches", 2026-10-13). Ist /v1/version nicht
erreichbar, gilt MAX_AGE; antwortet valorant-api.com gar nicht, ist ein alter
Eintrag als Notnagel noch bis STALE_MAX_AGE gut. Nach einem Verbindungsfehler
oder Timeout gilt die API DOWN_RETRY lang als weg: vorhandene Eintraege kommen
dann ohne Netz, statt dass jeder der neun Kataloge erst 20 s wartet.
"""
import hashlib
import json
import os
import threading
import time

import requests

from src.paths import data_dir

VERSION_URL = "https://valorant-api.com/v1/version"
API_MAX_AGE = 7 * 24 * 3600      # gleicher API-Stand: trotzdem einmal pro Woche neu
MAX_AGE = 24 * 3600              # API-Stand unbekannt (/v1/version nicht erreichbar)
STALE_MAX_AGE = 14 * 24 * 3600
TIMEOUT = 20
VERSION_TIMEOUT = 3              # harte Obergrenze INSGESAMT (requests' timeout gilt je Verbindungsversuch)
DOWN_RETRY = 600                 # nach Verbindungsfehler/Timeout so lange nicht neu versuchen

_mem = {}                    # url -> CachedResponse (pro Programmlauf)
_locks = {}
_locks_guard = threading.Lock()
_api = {"checked": False, "version": None}
_api_lock = threading.Lock()
_down = {"until": 0.0}


def _mark_down():
    _down["until"] = time.time() + DOWN_RETRY


def _is_down():
    return time.time() < _down["until"]


def _fetch_api_version(box):
    try:
        d = requests.get(VERSION_URL, timeout=VERSION_TIMEOUT).json().get("data") or {}
        v = "%s|%s" % (d.get("manifestId") or "", d.get("buildDate") or "")
        box["version"] = v if v != "|" else None
    except (requests.ConnectionError, requests.Timeout):
        box["down"] = True
    except (requests.RequestException, ValueError, AttributeError):
        pass


def _api_version():
    """Datenstand von valorant-api.com, einmal pro Programmlauf (None = unbekannt).

    In einem eigenen Thread mit fester Obergrenze: requests' timeout gilt je
    Verbindungsversuch und Adresse (valorant-api.com hat sechs), eine haengende
    erste Verbindung kostete im Test schon 10 s. Wer laenger braucht, gilt als
    "weg" - dann kommen die gespeicherten Daten sofort."""
    with _api_lock:
        if not _api["checked"]:
            _api["checked"] = True
            box = {"version": None, "down": False}
            t = threading.Thread(target=_fetch_api_version, args=(box,), daemon=True)
            t.start()
            t.join(VERSION_TIMEOUT)
            if t.is_alive() or box["down"]:
                _api["version"] = None
                _mark_down()
            else:
                _api["version"] = box["version"]
        return _api["version"]


class CachedResponse:
    """Das Stueck von requests.Response, das die Aufrufer brauchen - .json()
    liefert aber jedes Mal dasselbe, nur EINMAL geparste Objekt. Die Aufrufer
    lesen die Kataloge nur; nichts darf sie veraendern."""
    status_code = 200
    ok = True

    def __init__(self, data, expires=None):
        self._data = data
        self.expires = expires        # None = fuer den ganzen Programmlauf

    def json(self):
        return self._data


def _cache_file(url):
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    return os.path.join(data_dir(), "cache", "valorant-api", name + ".json")


def _read(url):
    try:
        with open(_cache_file(url), "r", encoding="utf-8") as fh:
            entry = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("url") != url or not isinstance(entry.get("data"), dict):
        return None
    if not isinstance(entry.get("savedAt"), (int, float)) or isinstance(entry.get("savedAt"), bool):
        return None
    return entry


def _write(url, data):
    path = _cache_file(url)
    # Eindeutiger Name: zwei vry.exe kurz gleichzeitig duerfen sich nicht die
    # halbe Datei gegenseitig ueberschreiben (der Lock gilt nur im Prozess)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"url": url, "savedAt": time.time(), "apiVersion": _api_version(), "data": data},
                      fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        # Cache ist nur eine Abkuerzung - ohne ihn laeuft alles wie frueher
        try:
            os.remove(tmp)
        except OSError:
            pass


def _age(entry):
    return time.time() - entry["savedAt"]


def _fresh(entry):
    age = _age(entry)
    if age < 0:
        return False
    api = _api_version()
    if api:
        return entry.get("apiVersion") == api and age < API_MAX_AGE
    return age < MAX_AGE


def _usable(data):
    # Eine leere Liste waehrend einer Stoerung nicht bis zu 7 Tage merken
    inner = data.get("data") if isinstance(data, dict) else None
    return isinstance(inner, (list, dict)) and len(inner) > 0 and data.get("status", 200) == 200


def _stale(url, entry):
    """Notnagel: alte Daten, aber nur DOWN_RETRY lang gemerkt - danach wird
    es erneut versucht, auch wenn vry.exe stundenlang laeuft."""
    hit = _mem[url] = CachedResponse(entry["data"], expires=time.time() + DOWN_RETRY)
    return hit


def _mem_hit(url):
    hit = _mem.get(url)
    if hit is not None and (hit.expires is None or time.time() < hit.expires):
        return hit
    return None


def get(url):
    """Wie requests.get(url) fuer einen valorant-api.com-Katalog, aber gecacht."""
    hit = _mem_hit(url)
    if hit is not None:
        return hit
    with _locks_guard:
        lock = _locks.setdefault(url, threading.Lock())
    with lock:                                # gleichzeitige Aufrufer holen nur einmal
        hit = _mem_hit(url)
        if hit is not None:
            return hit
        entry = _read(url)
        if entry and _fresh(entry):
            hit = _mem[url] = CachedResponse(entry["data"])
            return hit
        if entry and _is_down() and _age(entry) < STALE_MAX_AGE:
            return _stale(url, entry)         # API gerade weg: nicht erst 20 s warten
        try:
            r = requests.get(url, timeout=TIMEOUT)
            data = r.json() if r.status_code == 200 else None
        except (requests.ConnectionError, requests.Timeout) as e:
            _mark_down()
            r, data, err = None, None, e
        except (requests.RequestException, ValueError) as e:
            r, data, err = None, None, e
        else:
            err = None
        if _usable(data):
            _write(url, data)
            hit = _mem[url] = CachedResponse(data)
            return hit
        # valorant-api.com gerade nicht erreichbar oder kaputt: lieber alte
        # Daten als gar keine
        if entry and _age(entry) < STALE_MAX_AGE:
            return _stale(url, entry)
        if r is not None:
            return r                          # Verhalten wie vorher (Aufrufer pruefen "data")
        raise err
