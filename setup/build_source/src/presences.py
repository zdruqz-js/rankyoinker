import base64
import json
import time


# ============================ Patch-Hinweis ============================
# Originalquelle von vRY 2.94 (src/presences.py) mit einem Fallback fuer den
# Fall, dass die *eigene* Chat-Presence fehlt oder leer ist.
#
# Warum: Tools wie Deceive ("offline erscheinen") haengen sich in den
# Riot-Chat und ersetzen den eigenen Presence-Eintrag durch einen leeren
# "riot_client"-Eintrag. Das Original liest daraus den Spielzustand
# (MENUS/PREGAME/INGAME); faellt der Eintrag weg, bleibt vRY beim Start
# ewig in der Warteschleife haengen und laedt nie Spieler.
#
# Fix: fehlt die eigene Presence, wird der Zustand ueber die normale
# Spiel-API ermittelt (pregame/core-game/parties) - die funktioniert
# unabhaengig vom Chat. Ist die Presence normal vorhanden, aendert sich
# nichts am Verhalten.
#
# Diese Datei liegt bewusst neben der mitgelieferten presences.pyc: Python
# bevorzugt beim Import die .py-Datei, dadurch wirkt der Fix ohne Eingriff
# in library.zip oder vry.exe. Rueckgaengig machen = diese Datei loeschen.
# =======================================================================


class Presences:
    def __init__(self, Requests, log):
        self.Requests = Requests
        self.log = log
        # Fallback-Zustand (siehe Patch-Hinweis)
        self._fallback_logged = False
        self._party_cache = {"ts": 0.0, "data": None}
        self._state_cache = {"ts": 0.0, "data": None}

    def get_presence(self):
        presences = self.Requests.fetch(url_type="local", endpoint="/chat/v4/presences", method="get")
        if presences is None:
            return None
        return presences['presences']

    def get_game_state(self, presences):
        private_presence = self.get_private_presence(presences)
        if private_presence:
            # Temp fix: Riot is swapping between nested and flat API structures.
            # Check for nested structure.
            if "matchPresenceData" in private_presence:
                return private_presence["matchPresenceData"]["sessionLoopState"]
            # Check for flattened structure.
            elif "sessionLoopState" in private_presence:
                return private_presence["sessionLoopState"]
            else:
                # No known structure found, log and fail
                self.log("ERROR: Unknown presence API structure in 'get_game_state'.")
                return private_presence["matchPresenceData"]["sessionLoopState"]
        return None

    def get_private_presence(self, presences):
        entry = self.get_own_presence_entry(presences)
        if entry is not None:
            return json.loads(base64.b64decode(entry['private']))
        # Kein brauchbarer eigener Eintrag -> Fallback ueber die Spiel-API
        return self._private_presence_from_api()

    # ------------------------------------------------------------------
    # Hilfsfunktionen fuer den Fallback
    # ------------------------------------------------------------------

    def get_own_presence_entry(self, presences):
        """Eigenen VALORANT-Presence-Eintrag mit verwertbaren Daten suchen.

        Liefert None, wenn es keinen gibt - etwa weil gerade LoL laeuft oder
        weil Deceive den eigenen Eintrag geleert hat.
        """
        if not presences:
            return None
        for presence in presences:
            if presence.get('puuid') != self.Requests.puuid:
                continue
            # verhindert Abstuerze, wenn nebenbei LoL offen ist
            if presence.get("championId") is not None or presence.get("product") == "league_of_legends":
                continue
            # leere "riot_client"-Huelle (z. B. von Deceive) ueberspringen
            if presence.get("product") not in (None, "valorant"):
                continue
            if not presence.get('private'):
                continue
            return presence
        return None

    def has_own_presence(self, presences):
        """True, wenn die eigene Presence normal nutzbar ist."""
        return self.get_own_presence_entry(presences) is not None

    def get_own_party(self, max_age=5.0):
        """Eigene Party ueber die Spiel-API holen (kurz gecacht).

        Dient gleichzeitig als Test, ob ueberhaupt eine VALORANT-Sitzung
        laeuft: ohne Party gibt es kein Spiel.
        """
        now = time.time()
        if now - self._party_cache["ts"] < max_age:
            return self._party_cache["data"]

        party = None
        try:
            player = self.Requests.fetch(
                url_type="glz",
                endpoint=f"/parties/v1/players/{self.Requests.puuid}",
                method="get",
            )
            if isinstance(player, dict) and player.get("CurrentPartyID"):
                data = self.Requests.fetch(
                    url_type="glz",
                    endpoint=f"/parties/v1/parties/{player['CurrentPartyID']}",
                    method="get",
                )
                if isinstance(data, dict) and data.get("ID"):
                    party = data
        except Exception as e:
            self.log(f"Party konnte nicht ueber die Spiel-API geladen werden: {e}")

        self._party_cache = {"ts": now, "data": party}
        return party

    def get_api_game_state(self, prefer=None, max_age=2.0):
        """Spielzustand ohne Chat-Presence bestimmen.

        pregame/core-game antworten mit 404, wenn man nicht im jeweiligen
        Zustand ist - bleibt beides leer, sind wir im Menue.
        """
        now = time.time()
        if max_age > 0 and now - self._state_cache["ts"] < max_age:
            return self._state_cache["data"]

        order = ["INGAME", "PREGAME"] if prefer == "INGAME" else ["PREGAME", "INGAME"]
        state = "MENUS"
        for candidate in order:
            prefix = "/pregame/v1/players/" if candidate == "PREGAME" else "/core-game/v1/players/"
            response = self.Requests.fetch(
                url_type="glz",
                endpoint=prefix + self.Requests.puuid,
                method="get",
            )
            if isinstance(response, dict) and response.get("MatchID"):
                state = candidate
                break

        self._state_cache = {"ts": now, "data": state}
        return state

    def _private_presence_from_api(self):
        """Presence-Ersatz bauen, der dieselbe Struktur hat wie das Original."""
        party = self.get_own_party()
        if party is None:
            # Kein Spiel aktiv (oder API nicht erreichbar) - wie im Original
            return None

        if not self._fallback_logged:
            self.log(
                "Eigene Chat-Presence fehlt oder ist leer (z. B. weil Deceive laeuft). "
                "Spielzustand wird ab jetzt ueber die Spiel-API ermittelt."
            )
            self._fallback_logged = True

        state = self.get_api_game_state()

        members = party.get("Members") or []
        me = {}
        for member in members:
            if member.get("Subject") == self.Requests.puuid:
                me = member
                break

        party_id = party.get("ID", "")
        party_state = party.get("State", "DEFAULT")
        party_size = len(members)
        max_party_size = party.get("MaxPartySize") or 5
        queue_id = (party.get("MatchmakingData") or {}).get("QueueID", "")
        account_level = (me.get("PlayerIdentity") or {}).get("AccountLevel", 0)
        competitive_tier = (me.get("PlayerIdentity") or {}).get("CompetitiveTier", 0)

        return {
            "isValid": True,
            "isIdle": False,
            "queueId": queue_id,
            "provisioningFlow": "Invalid",
            "partyId": party_id,
            "partySize": party_size,
            "maxPartySize": max_party_size,
            "partyVersion": party.get("Version", 0),
            "partyOwnerMatchScoreAllyTeam": 0,
            "partyOwnerMatchScoreEnemyTeam": 0,
            "matchPresenceData": {
                "sessionLoopState": state,
                "provisioningFlow": "Invalid",
                "matchMap": "",
                "queueId": queue_id,
            },
            "partyPresenceData": {
                "partyId": party_id,
                "isPartyOwner": bool(me.get("IsOwner")),
                "partyState": party_state,
                "partyAccessibility": party.get("Accessibility", "CLOSED"),
                "partyLFM": False,
                "partyClientVersion": "unknown",
                "partyVersion": party.get("Version", 0),
                "partySize": party_size,
                "maxPartySize": max_party_size,
                "queueEntryTime": "0001.01.01-00.00.00",
                "isPartyCrossPlayEnabled": False,
                "isPlayerCrossPlayEnabled": False,
                "customGameName": "",
                "customGameTeam": "",
                "tournamentId": "",
                "rosterId": "",
                "partyOwnerSessionLoopState": state,
                "partyOwnerMatchMap": "",
                "partyOwnerProvisioningFlow": "Invalid",
                "partyOwnerMatchScoreAllyTeam": 0,
                "partyOwnerMatchScoreEnemyTeam": 0,
            },
            "playerPresenceData": {
                "playerCardId": "",
                "playerTitleId": "",
                "accountLevel": account_level,
                "competitiveTier": competitive_tier,
                "leaderboardPosition": 0,
            },
        }

    # ------------------------------------------------------------------

    def decode_presence(self, private):
        if "{" not in str(private) and private is not None and str(private) != "":
            decoded_party_presence = json.loads(base64.b64decode(str(private)).decode("utf-8"))
            if isinstance(decoded_party_presence, dict) and decoded_party_presence.get('isValid'):
                return decoded_party_presence
        return {
            "isValid": False,
            "partyId": 0,
            "partySize": 0,
            "partyVersion": 0,
        }

    def wait_for_presence(self, PlayersPuuids):
        while True:
            presence = self.get_presence()
            for puuid in PlayersPuuids:
                if puuid not in str(presence):
                    time.sleep(1)
                    continue
            break
