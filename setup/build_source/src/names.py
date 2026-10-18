import time

import requests

from src.requestsV import pd_wait, pd_feedback

NAME_TTL = 3600
NAME_TTL_HIDDEN = 600
_name_cache = {}      # puuid -> (gueltig bis, "Name#Tag")


class Names:

    def __init__(self, Requests, log):
        self.Requests = Requests
        self.log = log

    # pd_url is Riot's real, publicly-trusted cloud API (not the local self-
    # signed client API) - verify=True, see security report 2026-09-28.
    def _put(self, puuids, refresh=False):
        """name-service ueber die gemeinsame Bremse fuer pd.*.a.pvp.net (requestsV.py),
        mit Rueckmeldung - sonst bekam die Bremse 429/Sperren hier gar nicht mit."""
        ticket = pd_wait()
        try:
            headers = self.Requests.get_headers(refresh=True) if refresh else self.Requests.get_headers()
            response = requests.put(self.Requests.pd_url + "/name-service/v2/players", headers=headers, json=puuids, verify=True)
        except Exception:
            pd_feedback(None, ticket, self.log)
            raise
        pd_feedback(response, ticket, self.log)
        return response

    def get_name_from_puuid(self, puuid):
        response = self._put([puuid])
        return response.json()[0]["GameName"] + "#" + response.json()[0]["TagLine"]


    def get_multiple_names_from_puuid(self, puuids):
        """Namen merken (2.5.4-dev3): Sie aendern sich in einer Sitzung praktisch nie,
        vry.exe fragte sie aber bei jedem Zustandswechsel neu bei Riot (pd). Verborgene
        Namen (Streamer-Modus) nur kurz, wie NAME_TTL_HIDDEN im Dienst."""
        now = time.time()
        missing = [p for p in puuids if not (p in _name_cache and _name_cache[p][0] > now)]
        if missing:
            response = self._put(missing)

            if 'errorCode' in response.json():
                self.log(f'{response.json()["errorCode"]}, new token retrieved')
                response = self._put(missing, refresh=True)

            for player in response.json():
                hidden = not player.get("GameName")
                _name_cache[player["Subject"]] = (now + (NAME_TTL_HIDDEN if hidden else NAME_TTL),
                                                  f"{player['GameName']}#{player['TagLine']}")
        return {p: _name_cache[p][1] for p in puuids if p in _name_cache}

    def get_names_from_puuids(self, players):
        players_puuid = []
        for player in players:
            players_puuid.append(player["Subject"])
        return self.get_multiple_names_from_puuid(players_puuid)

    def get_players_puuid(self, Players):
        return [player["Subject"] for player in Players]
