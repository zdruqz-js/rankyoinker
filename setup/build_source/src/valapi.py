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
Eintrag als Notnagel noch bis STALE_MAX_AGE gut.
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
VERSION_TIMEOUT = 5

_mem = {}                    # url -> CachedResponse (pro Programmlauf)
_locks = {}
_locks_guard = threading.Lock()
_api = {"checked": False, "version": None}
_api_lock = threading.Lock()


def _api_version():
    """Datenstand von valorant-api.com, einmal pro Programmlauf (None = unbekannt)."""
    with _api_lock:
        if not _api["checked"]:
            _api["checked"] = True
            try:
                d = requests.get(VERSION_URL, timeout=VERSION_TIMEOUT).json().get("data") or {}
                v = "%s|%s" % (d.get("manifestId") or "", d.get("buildDate") or "")
                _api["version"] = v if v != "|" else None
            except (requests.RequestException, ValueError, AttributeError):
                _api["version"] = None
        return _api["version"]


class CachedResponse:
    """Das Stueck von requests.Response, das die Aufrufer brauchen - .json()
    liefert aber jedes Mal dasselbe, nur EINMAL geparste Objekt. Die Aufrufer
    lesen die Kataloge nur; nichts darf sie veraendern."""
    status_code = 200
    ok = True

    def __init__(self, data):
        self._data = data

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


def _fresh(entry):
    age = time.time() - float(entry.get("savedAt") or 0)
    if age < 0:
        return False
    api = _api_version()
    if api:
        return entry.get("apiVersion") == api and age < API_MAX_AGE
    return age < MAX_AGE


def _usable(data):
    return isinstance(data, dict) and isinstance(data.get("data"), (list, dict)) and data.get("status", 200) == 200


def get(url):
    """Wie requests.get(url) fuer einen valorant-api.com-Katalog, aber gecacht."""
    hit = _mem.get(url)
    if hit is not None:
        return hit
    with _locks_guard:
        lock = _locks.setdefault(url, threading.Lock())
    with lock:                                # gleichzeitige Aufrufer holen nur einmal
        hit = _mem.get(url)
        if hit is not None:
            return hit
        entry = _read(url)
        if entry and _fresh(entry):
            hit = _mem[url] = CachedResponse(entry["data"])
            return hit
        try:
            r = requests.get(url, timeout=TIMEOUT)
            data = r.json() if r.status_code == 200 else None
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
        if entry and time.time() - float(entry.get("savedAt") or 0) < STALE_MAX_AGE:
            hit = _mem[url] = CachedResponse(entry["data"])
            return hit
        if r is not None:
            return r                          # Verhalten wie vorher (Aufrufer pruefen "data")
        raise err
