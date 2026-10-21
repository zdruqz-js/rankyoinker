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
        self._party_cache = {"ts": 0.0, "data": None, "ttl": 0.0}
        self._party_seen = False
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
        if now - self._party_cache["ts"] < self._party_cache["ttl"]:
            return self._party_cache["data"]

        # Beim Spielstart ist der Party-Dienst oft noch nicht bereit. fetch()
        # schlief dann 10 s bis zum zweiten Versuch - die Startschleife in
        # main.py fragt aber ohnehin alle 2 s neu (Messung 2026-10-13: 10 s
        # Wartezeit vor dem ersten Heartbeat). Bis zur ersten erfolgreichen
        # Party deshalb nur ein Versuch und das "nichts" nur kurz merken; danach
        # wie bisher mit Wiederholung.
        quick = not self._party_seen
        attempts = 1 if quick else None
        party = None
        try:
            player = self.Requests.fetch(
                url_type="glz",
                endpoint=f"/parties/v1/players/{self.Requests.puuid}",
                method="get",
                max_attempts=attempts,
            )
            if isinstance(player, dict) and player.get("CurrentPartyID"):
                data = self.Requests.fetch(
                    url_type="glz",
                    endpoint=f"/parties/v1/parties/{player['CurrentPartyID']}",
                    method="get",
                    max_attempts=attempts,
                )
                if isinstance(data, dict) and data.get("ID"):
                    party = data
        except Exception as e:
            self.log(f"Party konnte nicht ueber die Spiel-API geladen werden: {e}")

        if party is not None:
            self._party_seen = True
        ttl = max_age if (party is not None or not quick) else min(max_age, 1.5)
        self._party_cache = {"ts": now, "data": party, "ttl": ttl}
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
        unclear = False
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
            # "nicht in diesem Zustand" nur bei einem echten 404 - eine 429/5xx/Cloudflare-Antwort
            # ist kein Beweis. Bis 2.5.7 machte ein Aussetzer mitten im Match daraus "MENUS":
            # Lobby-Heartbeat, Caches geleert, danach alle 10 Raenge neu von Riot (wie der Dienst
            # seit 2.5.5-dev11: MENUS nur bei 404/404)
            if not (isinstance(response, dict) and (response.get("errorCode") == "RESOURCE_NOT_FOUND"
                                                    or response.get("httpStatus") == 404)):
                unclear = True

        if state == "MENUS" and unclear:
            prev = self._state_cache.get("data")
            if prev:
                return prev                 # unklar -> beim letzten sicheren Stand bleiben
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

    def wait_for_presence(self, PlayersPuuids, timeout=3.0, settle=0.8):
        """Wartet kurz, bis die Chat-Presence der Mitspieler nachgezogen hat.

        War hier vorher kaputt: "break" stand ausserhalb der for-Schleife und
        lief darum IMMER nach genau einem Durchlauf, egal ob wirklich alle
        puuids schon da waren - "continue" im if sprang dabei nur zum
        naechsten puuid, nicht zurueck zum Schleifenkopf. Effekt: EIN
        Presence-Abruf, danach pro (zu diesem einen Zeitpunkt) fehlendem
        Spieler eine Sekunde sinnlos warten, und danach trotzdem weitermachen
        - unabhaengig davon, ob die Presence inzwischen tatsaechlich
        vollstaendig war.

        Die Folgefassung wartete dann wirklich, bis ALLE puuids auftauchen,
        mit 15 s Timeout. Das passiert aber praktisch nie: /chat/v4/presences
        enthaelt nur Freunde (und einen selbst), fremde Mitspieler tauchen dort
        nie auf (im laufenden Match nachgeprueft, 2026-10-13). Damit lief bei
        JEDEM Match- und Agentenauswahl-Start der volle Timeout ab - im
        Timing-Log "wait_for_presence fertig: +15.62s", der mit Abstand
        groesste Einzelposten beim Laden.

        Jetzt: zurueck, sobald alle da sind ODER sich die Menge der sichtbaren
        Mitspieler `settle` Sekunden lang nicht mehr veraendert hat (dann kommt
        auch nichts mehr). `timeout` bleibt als Sicherheitsventil.
        """
        start = time.time()
        wanted = set(PlayersPuuids)
        seen, changed_at = None, start
        while True:
            haystack = str(self.get_presence())
            now_seen = {puuid for puuid in wanted if puuid in haystack}
            if now_seen == wanted:
                return
            now = time.time()
            if now_seen != seen:
                seen, changed_at = now_seen, now
            elif now - changed_at >= settle:
                return
            if now - start > timeout:
                return
            time.sleep(0.25)
