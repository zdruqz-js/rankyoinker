import requests

from src.requestsV import pd_wait, pd_feedback


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
        """Bis 2.5.7 beendete eine Cloudflare-HTML-Seite (JSONDecodeError) oder ein fehlender Spieler in
        der Antwort vry.exe ganz (KeyError weiter oben). Jetzt: einmal neu versuchen, fehlende Namen leer."""
        data = None
        for attempt in range(2):
            try:
                response = self._put(puuids, refresh=attempt > 0)
                data = response.json()
            except Exception as e:
                self.log(f"name-service: {type(e).__name__}")
                data = None
            if isinstance(data, dict) and 'errorCode' in data:
                self.log(f'{data["errorCode"]}, new token retrieved')
                data = None
            if isinstance(data, list):
                break
        name_dict = {}
        for player in data if isinstance(data, list) else []:
            try:
                name_dict[player["Subject"]] = f"{player['GameName']}#{player['TagLine']}"
            except (KeyError, TypeError):
                continue
        for puuid in puuids:
            name_dict.setdefault(puuid, "")
        return name_dict

    def get_names_from_puuids(self, players):
        players_puuid = []
        for player in players:
            players_puuid.append(player["Subject"])
        return self.get_multiple_names_from_puuid(players_puuid)

    def get_players_puuid(self, Players):
        return [player["Subject"] for player in Players]
