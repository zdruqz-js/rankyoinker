"""Rang und Stats des LAUFENDEN Matches ueber einen Neustart von vry.exe retten.

Ein Update oder Neustart mitten im Match liess vry.exe Rang, Peak und Stats
aller zehn Spieler erneut bei Riot abfragen (rund 30 pd-Anfragen auf einen
Schlag, genau der Stoss, der Cloudflare-Sperren ausloest). Seit 2.4.9 liegt
der Stand in DATA_DIR/match-snapshot-vry.json, immer nur fuer EIN Match:
main.py vergleicht beim Start zuerst die Match-ID und uebernimmt nur bei
Gleichheit; im Menue wird die Datei geloescht. Gegenstueck im lokalen
Dienst: match-snapshot.json (vry_log_server.py, _snapshot_*).
"""
import json
import os
import threading
import time

from src.paths import data_dir

FILE = os.path.join(data_dir(), "match-snapshot-vry.json")
_KEYS = ("playerRank", "previousPlayerRank", "ppstats")
_lock = threading.Lock()


def _usable(entry):
    # Gescheiterte Rang-Abfragen (z. B. waehrend einer Riot-Sperre) nicht
    # festschreiben - die sollen nach einem Neustart erneut versucht werden.
    if not isinstance(entry, dict) or any(k not in entry for k in _KEYS):
        return False
    rank = entry.get("playerRank")
    return isinstance(rank, dict) and rank.get("statusgood") is True


def save(match_id, players):
    """players: {puuid: {"playerRank", "previousPlayerRank", "ppstats", "ts"}}"""
    if not match_id:
        return
    items = list(players.items())   # Kopie - andere Threads fuellen den Cache weiter
    data = {"match_id": match_id, "saved_at": time.time(),
            "players": {k: {kk: v[kk] for kk in _KEYS} for k, v in items if _usable(v)}}
    with _lock:
        tmp = FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, default=str)
            os.replace(tmp, FILE)
        except (OSError, TypeError, ValueError):
            pass


def load(match_id):
    """Gespeicherte Spieler, wenn die Datei zu genau diesem Match gehoert, sonst {}."""
    if not match_id:
        return {}
    with _lock:
        try:
            with open(FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
    if not isinstance(data, dict) or data.get("match_id") != match_id:
        return {}
    now = time.time()
    return {k: dict(v, ts=now) for k, v in (data.get("players") or {}).items() if _usable(v)}


def clear():
    with _lock:
        for path in (FILE, FILE + ".tmp"):
            try:
                os.remove(path)
            except OSError:
                pass
