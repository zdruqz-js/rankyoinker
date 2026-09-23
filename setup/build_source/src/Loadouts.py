import time
import requests
from colr import color
from src.constants import sockets, hide_names
import json

# Warden (13.06-Patch, 2026-09-22/23) fehlt bei valorant-api.com noch (Community-
# Projekt, haengt bei ganz neuen Inhalten ein paar Tage hinterher - dasselbe
# Muster wie zuvor bei Maps/Spielmodus/Fraktionen). IDs aus einem echten Live-
# Loadout (Community, 2026-09-23); Standard-Skin ist bislang die einzige
# bestaetigte Option (zwei unabhaengige Signale: weder in den Skin-
# Entitlements eines Accounts mit hunderten Skins sonst, noch auf der
# offiziellen Waffenseite taucht ein Kauf-Skin fuer Warden auf). Icon
# offiziell bei Riot selbst gehostet. Analog zu addWardenFallback() in
# index.html und _with_warden_fallback() in vry_log_server.py - drei
# getrennte Stellen holen sich alle unabhaengig dieselben valorant-api.com-
# Waffendaten, brauchen also alle dieselbe Ergaenzung.
WARDEN_UUID = "8db0a1bf-4a50-832a-4566-faaaa6d250ca"
WARDEN_STANDARD_SKIN = "61d99a36-4033-0fa9-2c94-71aaf901e120"
WARDEN_STANDARD_LEVEL = "45a2214f-4730-8c89-b0a4-a7a2b5125ac8"
WARDEN_ICON = "https://wiki.playvalorant.com/en-us/images/thumb/Warden.png/512px-Warden.png"


def _weapons_with_warden(weapons_data):
    if any((w.get("uuid") or "").lower() == WARDEN_UUID for w in weapons_data):
        return weapons_data   # valorant-api.com hat nachgezogen - nichts zu tun
    return weapons_data + [{
        "uuid": WARDEN_UUID, "displayName": "Warden", "displayIcon": WARDEN_ICON,
        "skins": [{
            "uuid": WARDEN_STANDARD_SKIN, "displayName": "Standard Warden",
            "displayIcon": WARDEN_ICON, "chromas": [],
            "levels": [{"uuid": WARDEN_STANDARD_LEVEL, "displayIcon": WARDEN_ICON}],
        }],
    }]


class Loadouts:
    def __init__(self, Requests, log, colors, Server, current_map):

        self.Requests = Requests
        self.log = log
        self.colors = colors
        self.Server = Server
        self.current_map = current_map
        # Statische valorant-api.com-Kataloge (Waffen/Sprays/Buddies/Agenten/
        # Titel/Spielerkarten) aendern sich hoechstens mal pro Patch, nicht
        # zwischen zwei Matches oder gar zwischen zwei Nachversuchen
        # innerhalb DESSELBEN Matches. Wurden hier vorher bei JEDEM Aufruf von
        # get_match_loadouts komplett neu geholt (bis zu 7 Anfragen) - bei
        # bis zu 4 Aufrufen pro Match (1 Versuch + 3 Nachversuche bei
        # unvollstaendigen Loadouts) ging dafuer unnoetig Zeit drauf. Jetzt
        # einmal pro Programmlauf gecacht.
        self._ref_cache = {}

    def _cached_get(self, url):
        if url not in self._ref_cache:
            self._ref_cache[url] = requests.get(url)
        return self._ref_cache[url]

    def get_match_loadouts(self, match_id, players, weaponChoose, valoApiSkins, names, state="game"):
        playersBackup = players
        weaponLists = {}
        valApiWeapons = self._cached_get(
            "https://valorant-api.com/v1/weapons").json()
        if state == "game":
            team_id = "Blue"
            PlayerInventorys = self.Requests.fetch(
                "glz", f"/core-game/v1/matches/{match_id}/loadouts", "get")
        elif state == "pregame":
            pregame_stats = players
            players = players["AllyTeam"]["Players"]
            team_id = pregame_stats['Teams'][0]['TeamID']
            PlayerInventorys = self.Requests.fetch(
                "glz", f"/pregame/v1/matches/{match_id}/loadouts", "get")

        # subject (player UUID) -> loadout lookup
        loadout_by_subject = {}
        for loadout_entry in PlayerInventorys["Loadouts"]:
            subj = loadout_entry.get("Subject", "").lower()
            # if player has an agent != spectator
            char_id = loadout_entry.get("CharacterID", "")
            if subj and char_id:
                loadout_by_subject[subj] = loadout_entry["Loadout"] if state == "game" else loadout_entry

        for player in players:
            subj = player.get("Subject", "").lower()
            inv = loadout_by_subject.get(subj)
            if inv is None:
                continue
            for weapon in valApiWeapons["data"]:
                if weapon["displayName"].lower() == weaponChoose.lower():
                    skin_id = \
                        inv["Items"][weapon["uuid"].lower()]["Sockets"]["bcef87d6-209b-46c6-8b19-fbe40bd95abc"]["Item"][
                            "ID"]
                    json_data = valoApiSkins.json()

                    if "data" not in json_data:
                        self.log("Skins API response missing 'data'.")
                        return None

                    for skin in json_data["data"]:
                        if skin_id.lower() == skin["uuid"].lower():
                            rgb_color = self.colors.get_rgb_color_from_skin(
                                skin["uuid"].lower(), valoApiSkins)
                            skin_display_name = skin["displayName"].replace(
                                f" {weapon['displayName']}", "")
                            # if rgb_color is not None:
                            weaponLists.update({player["Subject"]: color(
                                skin_display_name, fore=rgb_color)})
                            # else:
                            #     weaponLists.update({player["Subject"]: color(skin["Name"], fore=rgb_color)})
        final_json = self.convertLoadoutToJsonArray(
            PlayerInventorys, playersBackup, state, names)
        # self.log(f"json for website: {final_json}")
        self.Server.send_payload("matchLoadout", final_json)
        return [weaponLists, final_json]

    # this will convert valorant loadouts to json with player names
    def convertLoadoutToJsonArray(self, PlayerInventorys, players, state, names):
        # get agent dict from main in future
        # names = self.namesClass.get_names_from_puuids(players)
        valoApiSprays = self._cached_get("https://valorant-api.com/v1/sprays")
        valoApiWeapons = self._cached_get("https://valorant-api.com/v1/weapons")
        valoApiBuddies = self._cached_get("https://valorant-api.com/v1/buddies")
        valoApiAgents = self._cached_get("https://valorant-api.com/v1/agents")
        valoApiTitles = self._cached_get(
            "https://valorant-api.com/v1/playertitles")
        valoApiPlayerCards = self._cached_get(
            "https://valorant-api.com/v1/playercards")

        final_final_json = {"Players": {},
                            "time": int(time.time()),
                            "map": self.current_map}

        final_json = final_final_json["Players"]
        if state == "game":
            PlayerInventorys = PlayerInventorys["Loadouts"]

            # subject (player UUID) -> loadout lookup
            loadout_by_subject = {}
            for entry in PlayerInventorys:
                subj = entry.get("Subject", "").lower()
                # if player has an agent != spectator
                char_id = entry.get("CharacterID", "")
                if subj and char_id:
                    loadout_by_subject[subj] = entry["Loadout"]

            for player in players:
                subject = player["Subject"]
                subj = subject.lower()
                loadout_entry = loadout_by_subject.get(subj)

                final_json.update(
                    {
                        subject: {}
                    }
                )

                # skip if not found
                if loadout_entry is None:
                    continue

                PlayerInventory = loadout_entry

                # creates name field
                if hide_names:
                    for agent in valoApiAgents.json()["data"]:
                        if agent["uuid"] == player["CharacterID"]:
                            final_json[subject].update(
                                {"Name": agent["displayName"]})
                else:
                    final_json[subject].update({"Name": names[subject]})

                # creates team field
                final_json[subject].update({"Team": player["TeamID"]})

                # create spray field
                final_json[subject].update({"Sprays": {}})
                # append sprays to field

                final_json[subject].update(
                    {"Level": player["PlayerIdentity"]["AccountLevel"]})

                for title in valoApiTitles.json()["data"]:
                    if title["uuid"] == player["PlayerIdentity"]["PlayerTitleID"]:
                        final_json[subject].update(
                            {"Title": title["titleText"]})

                for PCard in valoApiPlayerCards.json()["data"]:
                    if PCard["uuid"] == player["PlayerIdentity"]["PlayerCardID"]:
                        final_json[subject].update(
                            {"PlayerCard": PCard["largeArt"]})

                for agent in valoApiAgents.json()["data"]:
                    if agent["uuid"] == player["CharacterID"]:
                        final_json[subject].update(
                            {"AgentArtworkName": agent["displayName"] + "Artwork"})
                        final_json[subject].update(
                            {"Agent": agent["displayIcon"]})

                spray_selections = [
                    s for s in PlayerInventory.get("Expressions", {}).get("AESSelections", [])
                    if s.get("TypeID") == "d5f120f8-ff8c-4aac-92ea-f2b5acbe9475"
                ]
                for j, spray in enumerate(spray_selections):
                    final_json[subject]["Sprays"].update({j: {}})
                    for sprayValApi in valoApiSprays.json()["data"]:
                        if spray["AssetID"].lower() == sprayValApi["uuid"].lower():
                            final_json[subject]["Sprays"][j].update({
                                "displayName": sprayValApi["displayName"],
                                "displayIcon": sprayValApi["displayIcon"],
                                "fullTransparentIcon": sprayValApi["fullTransparentIcon"]
                            })

                # create weapons field
                final_json[subject].update({"Weapons": {}})

                for skin in PlayerInventory["Items"]:

                    # create skin field
                    final_json[subject]["Weapons"].update({skin: {}})

                    for socket in PlayerInventory["Items"][skin]["Sockets"]:
                        # predefined sockets
                        for var_socket in sockets:
                            if socket == sockets[var_socket]:
                                final_json[subject]["Weapons"][skin].update(
                                    {
                                        var_socket: PlayerInventory["Items"][skin]["Sockets"][socket]["Item"]["ID"]
                                    }
                                )

                    # create buddy field
                    # self.log("predefined sockets")
                    # final_json[subject]["Weapons"].update({skin: {}})

                    # buddies
                    for socket in PlayerInventory["Items"][skin]["Sockets"]:
                        if sockets["skin_buddy"] == socket:
                            for buddy in valoApiBuddies.json()["data"]:
                                if buddy["uuid"] == PlayerInventory["Items"][skin]["Sockets"][socket]["Item"]["ID"]:
                                    final_json[subject]["Weapons"][skin].update(
                                        {
                                            "buddy_displayIcon": buddy["displayIcon"]
                                        }
                                    )

                    # append names to field
                    for weapon in _weapons_with_warden(valoApiWeapons.json()["data"]):
                        if skin == weapon["uuid"]:
                            final_json[subject]["Weapons"][skin].update(
                                {
                                    "weapon": weapon["displayName"]
                                }
                            )
                            for skinValApi in weapon["skins"]:
                                if skinValApi["uuid"] == PlayerInventory["Items"][skin]["Sockets"][sockets["skin"]]["Item"]["ID"]:
                                    final_json[subject]["Weapons"][skin].update(
                                        {
                                            "skinDisplayName": skinValApi["displayName"]
                                        }
                                    )
                                    for chroma in skinValApi["chromas"]:
                                        if chroma["uuid"] == PlayerInventory["Items"][skin]["Sockets"][sockets["skin_chroma"]]["Item"]["ID"]:
                                            if chroma["displayIcon"] != None:
                                                final_json[subject]["Weapons"][skin].update(
                                                    {
                                                        "skinDisplayIcon": chroma["displayIcon"]
                                                    }
                                                )
                                            elif chroma["fullRender"] != None:
                                                final_json[subject]["Weapons"][skin].update(
                                                    {
                                                        "skinDisplayIcon": chroma["fullRender"]
                                                    }
                                                )
                                            elif skinValApi["displayIcon"] != None:
                                                final_json[subject]["Weapons"][skin].update(
                                                    {
                                                        "skinDisplayIcon": skinValApi["displayIcon"]
                                                    }
                                                )
                                            else:
                                                final_json[subject]["Weapons"][skin].update(
                                                    {
                                                        "skinDisplayIcon": skinValApi["levels"][0]["displayIcon"]
                                                    }
                                                )
                                    if skinValApi["displayName"].startswith("Standard") or skinValApi["displayName"].startswith("Melee"):
                                        final_json[subject]["Weapons"][skin]["skinDisplayIcon"] = weapon["displayIcon"]

        return final_final_json
