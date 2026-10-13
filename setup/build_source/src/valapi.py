"""Plattencache fuer die statischen valorant-api.com-Kataloge.

vry.exe holte bei JEDEM Start alle Kataloge neu (Skins, Waffen, Sprays,
Buddies, Agenten, Titel, Spielerkarten, Karten - zusammen gut 10 MB) und las
die grossen davon im Match fuer jeden Spieler erneut ein (requests'
Response.json() parst bei jedem Aufruf neu). Die Daten aendern sich aber nur
mit einem Patch.

Jetzt: einmal geholt, auf Platte unter DATA_DIR/cache/valorant-api/ gemerkt
und pro Programmlauf nur einmal geparst. Neu geholt wird, wenn sich die
Spielversion aendert (aus ShooterGame.log, siehe set_client_version) oder der
Eintrag aelter als MAX_AGE ist - valorant-api.com zieht neue Inhalte oft erst
Stunden nach einem Patch nach. Antwortet valorant-api.com nicht, gilt ein
alter Eintrag als Notnagel noch bis STALE_MAX_AGE.
"""
import hashlib
import json
import os
import threading
import time

import requests

from src.paths import data_dir

MAX_AGE = 24 * 3600
STALE_MAX_AGE = 14 * 24 * 3600
TIMEOUT = 20

_client_version = None
_mem = {}                    # url -> CachedResponse (pro Programmlauf)
_locks = {}
_locks_guard = threading.Lock()


def set_client_version(version):
    """Spielversion als Cache-Schluessel (None = nur nach Alter erneuern)."""
    global _client_version
    _client_version = version or None


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
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"url": url, "savedAt": time.time(), "clientVersion": _client_version, "data": data},
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
    if age < 0 or age > MAX_AGE:
        return False
    return not _client_version or entry.get("clientVersion") == _client_version


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
