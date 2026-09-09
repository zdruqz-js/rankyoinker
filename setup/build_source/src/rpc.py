import json
import time
import asyncio
import threading
import atexit
from typing import Any, Dict, Optional

from pypresence import AioPresence
from pypresence.exceptions import DiscordNotFound, InvalidID, PipeClosed


# ============================ Patch-Hinweis ============================
# Originalquelle von vRY 2.94 (src/rpc.py), fuer den RankYoinker-Rebrand
# angepasst:
#
#   - Der Rich-Presence-Button "What's this? 👀" zeigt jetzt auf
#     rankyoinker.de statt auf das Original-GitHub-Repo.
#   - Alle sichtbaren Statustexte (Party/Lobby/Agentenauswahl/Idle/Button)
#     folgen jetzt der in config.json hinterlegten Sprache (RPC_STRINGS
#     unten) statt fest auf Deutsch zu stehen. config.json wird dafuer bei
#     JEDEM Update-Zyklus neu gelesen (siehe _current_lang()) - ein
#     Sprachwechsel im Browser (setLang() in index.html, schreibt ueber
#     vry_log_server.py's /api/set-lang in dieselbe config.json) wirkt sich
#     so ohne Neustart von vry.exe aus.
#
# Original + Quellcode: https://github.com/zayKenyon/VALORANT-rank-yoinker
# (ISC-Lizenz, Copyright (c) 2021-2023 Zay Kenyon and Contributors)
#
# Diese Datei liegt bewusst neben der mitgelieferten rpc.pyc: Python
# bevorzugt beim Import die .py-Datei, dadurch wirkt der Patch ohne
# Eingriff in library.zip oder vry.exe. Rueckgaengig machen = diese Datei
# loeschen.
# =======================================================================

RPC_STRINGS: Dict[str, Dict[str, str]] = {
    "de": {
        "whats_this": "Was ist das? 👀",
        "in_party": "In einer Party ({size} von {max})",
        "open_party": "Offene Party",
        "closed_party": "Geschlossene Party",
        "count_suffix": "({size} von {max})",
        "lobby_prefix": " Lobby - {gamemode}",
        "agent_select_prefix": "Agentenauswahl - {gamemode}",
        "valorant_away": "VALORANT - Abwesend",
        "valorant_online": "VALORANT - Online",
        "on_the_range": "Auf der Range",
        "custom_game": "Custom Game",
    },
    "en": {
        "whats_this": "What's that? 👀",
        "in_party": "In a party ({size} of {max})",
        "open_party": "Open party",
        "closed_party": "Closed party",
        "count_suffix": "({size} of {max})",
        "lobby_prefix": " Lobby - {gamemode}",
        "agent_select_prefix": "Agent select - {gamemode}",
        "valorant_away": "VALORANT - Away",
        "valorant_online": "VALORANT - Online",
        "on_the_range": "On the Range",
        "custom_game": "Custom Game",
    },
    "pl": {
        "whats_this": "Co to jest? 👀",
        "in_party": "W drużynie ({size} z {max})",
        "open_party": "Otwarta drużyna",
        "closed_party": "Zamknięta drużyna",
        "count_suffix": "({size} z {max})",
        "lobby_prefix": " Lobby - {gamemode}",
        "agent_select_prefix": "Wybór agenta - {gamemode}",
        "valorant_away": "VALORANT - Zaraz wracam",
        "valorant_online": "VALORANT - Online",
        "on_the_range": "Na strzelnicy",
        "custom_game": "Gra niestandardowa",
    },
    "fr": {
        "whats_this": "C'est quoi ? 👀",
        "in_party": "En groupe ({size} sur {max})",
        "open_party": "Groupe ouvert",
        "closed_party": "Groupe fermé",
        "count_suffix": "({size} sur {max})",
        "lobby_prefix": " Lobby - {gamemode}",
        "agent_select_prefix": "Sélection d'agent - {gamemode}",
        "valorant_away": "VALORANT - Absent",
        "valorant_online": "VALORANT - En ligne",
        "on_the_range": "Sur le Stand de tir",
        "custom_game": "Partie personnalisée",
    },
    "es": {
        "whats_this": "¿Qué es esto? 👀",
        "in_party": "En un grupo ({size} de {max})",
        "open_party": "Grupo abierto",
        "closed_party": "Grupo cerrado",
        "count_suffix": "({size} de {max})",
        "lobby_prefix": " Lobby - {gamemode}",
        "agent_select_prefix": "Selección de agente - {gamemode}",
        "valorant_away": "VALORANT - Ausente",
        "valorant_online": "VALORANT - En línea",
        "on_the_range": "En el Campo de tiro",
        "custom_game": "Partida personalizada",
    },
    "tr": {
        "whats_this": "Bu ne? 👀",
        "in_party": "Bir toplulukta ({size}/{max})",
        "open_party": "Açık topluluk",
        "closed_party": "Kapalı topluluk",
        "count_suffix": "({size}/{max})",
        "lobby_prefix": " Lobi - {gamemode}",
        "agent_select_prefix": "Ajan seçimi - {gamemode}",
        "valorant_away": "VALORANT - Uzakta",
        "valorant_online": "VALORANT - Çevrimiçi",
        "on_the_range": "Atış Poligonunda",
        "custom_game": "Özel oyun",
    },
}


def _current_lang() -> str:
    """Liest die App-Sprache direkt aus config.json (relativ zum Arbeits-
    verzeichnis, genau wie src/config.py das schon fuer alles andere tut).
    Bewusst OHNE Cache: config.json ist winzig, ein Lesevorgang pro Update-
    Zyklus (max. alle paar Sekunden) faellt nicht ins Gewicht, macht dafuer
    einen Sprachwechsel sofort wirksam statt erst nach einem Neustart."""
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        lang = str((data or {}).get("lang", "de")).lower()
    except Exception:
        lang = "de"
    return lang if lang in RPC_STRINGS else "de"


def _rpc_strings() -> Dict[str, str]:
    return RPC_STRINGS[_current_lang()]


class Rpc:
    def __init__(self, map_dict, gamemodes, colors, log):
        self.log = log
        self.map_dict = map_dict
        self.gamemodes = gamemodes
        self.colors = colors

        # Config
        self.client_id = "1012402211134910546"

        self.discord_running: bool = False
        self.last_presence_data: Dict[str, Any] = {}

        self._rpc: Optional[AioPresence] = None
        self._connected: bool = False

        self._data: Dict[str, Any] = {"agent": None, "rank": None, "rank_name": None}
        self._desired_presence: Dict[str, Any] = {}

        # Shadows (before the loop)
        self._shadow_data = dict(self._data)
        self._shadow_presence = dict(self._desired_presence)

        # Update/reconnect controls
        self._update_event: Optional[asyncio.Event] = None
        self._min_interval: float = 1.0
        self._last_sent_ts: float = 0.0
        self._backoff: float = 1.0  # 1, 2, 4, ... até 30
        self._last_loop_state: Optional[str] = None
        self.start_time: float = time.time()
        self._max_backoff_notice: bool = False

        # Base payload - "buttons" wird in _finalize_payload() live pro
        # Update-Zyklus mit dem aktuell gewaehlten Sprachlabel ergaenzt, hier
        # bewusst KEIN Text mehr vorbelegt.
        self._base_payload: Dict[str, Any] = {}
        # Cache last payload (for dedupe)
        self._last_sent_payload: Optional[Dict[str, Any]] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner_task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None

        self._start_thread()
        atexit.register(self.shutdown)

    def set_data(self, data: Dict[str, Any]):
        if not isinstance(data, dict):
            return

        # Update shadow
        self._shadow_data = {**self._shadow_data, **data}

        if self._loop and self._loop.is_running():
            def _apply():
                self._data = {**self._data, **data}
                self.log("RPC: New data set")
                if self._update_event:
                    self._update_event.set()
            self._loop.call_soon_threadsafe(_apply)
        else:
            self.log("RPC: New data set (queued)")
            self.set_rpc(self.last_presence_data)

    def set_rpc(self, presence: Dict[str, Any]):
        presence = presence or {}
        self.last_presence_data = presence
        self._shadow_presence = presence

        if self._loop and self._loop.is_running():
            def _apply():
                self._desired_presence = presence
                if self._update_event:
                    self._update_event.set()
            self._loop.call_soon_threadsafe(_apply)

    def shutdown(self):
        if not self._thread or not self._thread.is_alive():
            return

        def _cancel_runner():
            if self._runner_task and not self._runner_task.done():
                self._runner_task.cancel()

        try:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(_cancel_runner)
                self._thread.join(timeout=3.0)
        except Exception:
            pass

    def _start_thread(self):
        if self._thread and self._thread.is_alive():
            return

        def _thread_target():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._update_event = asyncio.Event()

            # Initializes internal state from the shadows
            self._data = dict(self._shadow_data)
            self._desired_presence = dict(self._shadow_presence)

            self._runner_task = self._loop.create_task(self._run())

            try:
                self._loop.run_until_complete(self._runner_task)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                try:
                    self.log(f"RPC: Loop crashed: {e}")
                except Exception:
                    pass
            finally:
                try:
                    self._loop.run_until_complete(self._safe_close())
                except Exception:
                    pass
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                except Exception:
                    pass
                try:
                    self._loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_thread_target, name="RpcAioPresenceLoop", daemon=True)
        self._thread.start()

    async def _run(self):
        await self._connect()

        while True:
            try:
                # Waits update signal or sends a keep-alive
                try:
                    await asyncio.wait_for(self._update_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass

                self._update_event.clear()

                if not self._connected:
                    await self._connect()

                # Rate-limit
                now = time.monotonic()
                if now - self._last_sent_ts < self._min_interval:
                    await asyncio.sleep(self._min_interval - (now - self._last_sent_ts))

                dynamic = self._build_payload(self._desired_presence, self._data)
                if dynamic is None:
                    continue

                payload = self._finalize_payload(dynamic)

                # Dedupe: only sends if changed
                if payload == self._last_sent_payload:
                    continue

                await self._rpc.update(**payload)
                self._last_sent_ts = time.monotonic()
                self._last_sent_payload = payload

                # Logs
                # Handle both flattened and nested API structures (temp fix)
                state = None
                if "matchPresenceData" in self._desired_presence: # Check for nested structure first
                    state = (self._desired_presence.get("matchPresenceData") or {}).get("sessionLoopState")
                elif "sessionLoopState" in self._desired_presence: # Check for flattened structure
                    state = self._desired_presence.get("sessionLoopState")

                if state == "INGAME":
                    self.log("RPC: in-game data update")
                elif state == "MENUS":
                    self.log("RPC: menu data updated")
                elif state == "PREGAME":
                    self.log("RPC: agent-select data update")

            except asyncio.CancelledError:
                break

            except (PipeClosed, ConnectionError, OSError, InvalidID) as e:
                self.log(f"RPC: Transport lost: {e}")
                await self._safe_close()
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 30.0)

            except Exception as e:
                self.log(f"RPC: Unexpected error in main loop: {e}")
                await self._safe_close()
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 30.0)

    async def _connect(self):
        while True:
            try:
                if self._rpc is None:
                    self._rpc = AioPresence(self.client_id)

                await self._rpc.connect()
                self._connected = True
                self.discord_running = True
                self._backoff = 1.0
                self._max_backoff_notice = False
                self.log("RPC: Connected to discord")
                return
            except asyncio.CancelledError:
                raise
            except DiscordNotFound:
                self._connected = False
                self.discord_running = False
                if self._backoff < 30.0:
                    self.log(f"RPC: Discord not found, retrying in {self._backoff:.1f}s...")
                    self._max_backoff_notice = False
                else:
                    if not self._max_backoff_notice:
                        self.log("RPC: Waiting for Discord client, Retry loop initiated (30s).")
                        self._max_backoff_notice = True
            except Exception as e:
                self._connected = False
                self.discord_running = False
                self.log(f"RPC: Failed to connect: {e}")

            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, 30.0)

    async def _safe_close(self):
        if self._rpc and self._connected:
            try:
                await self._rpc.close()
                self.log("RPC: Connection closed gracefully.")
            except Exception as e:
                self.log(f"RPC: close() reported: {e}")

        self._connected = False
        self.discord_running = False
        self._rpc = None
        self._last_sent_payload = None  # Forces a full resend on reconnect

    def _finalize_payload(self, dynamic: Dict[str, Any]) -> Dict[str, Any]:
        buttons = {"buttons": [{"label": _rpc_strings()["whats_this"], "url": "https://rankyoinker.de"}]}
        merged = {**self._base_payload, **buttons, **dynamic}
        return {k: v for k, v in merged.items() if v is not None}

    def _build_payload(self, presence: Dict[str, Any], data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not presence or not presence.get("isValid"):
            return None

        S = _rpc_strings()

        # Temp fix: Riot is swapping between nested and flat API structures.
        session_state = None
        match_map = ""
        party_size = 0
        max_party = 0
        party_access = ""
        party_state = ""

        if "matchPresenceData" in presence: # Check for nested structure
            match_data = presence.get("matchPresenceData", {}) or {}
            party_data = presence.get("partyPresenceData", {}) or {}

            session_state = match_data.get("sessionLoopState")
            match_map = match_data.get("matchMap", "")
            party_size = party_data.get("partySize")
            max_party = party_data.get("maxPartySize")
            party_access = party_data.get("partyAccessibility")
            party_state = party_data.get("partyState")
        elif "sessionLoopState" in presence: # Check for flattened structure
            session_state = presence.get("sessionLoopState")
            match_map = presence.get("matchMap", "")
            party_size = presence.get("partySize")
            max_party = presence.get("maxPartySize")
            party_access = presence.get("partyAccessibility")
            party_state = presence.get("partyState")
        else:
            # No known structure found, log and fail
            self.log("ERROR: Unknown presence API structure in 'rpc._build_payload'.")
            session_state = presence["matchPresenceData"]["sessionLoopState"]


        if not session_state:
            return None

        if session_state != self._last_loop_state:
            self.start_time = time.time()
            self._last_loop_state = session_state

        if session_state == "INGAME":
            if data.get("agent") in (None, ""):
                agent_img = None
                agent = None
            else:
                agent_name = (self.colors.agent_dict.get(data.get("agent", "").lower())
                              if getattr(self.colors, "agent_dict", None) else None)
                agent = agent_name
                agent_img = agent_name.lower().replace("/", "") if agent_name else None

            gamemode = S["custom_game"] if presence.get("provisioningFlow") == "CustomGame" else self.gamemodes.get(presence.get("queueId"))

            ally = presence.get("partyOwnerMatchScoreAllyTeam")
            enemy = presence.get("partyOwnerMatchScoreEnemyTeam")
            details = f"{gamemode} // {ally} - {enemy}"

            match_map = (match_map or "").lower()
            mapText = self.map_dict.get(match_map)
            if mapText == "The Range":
                mapImage = "splash_range_square"
                details = S["on_the_range"]
                agent_img = str(data.get("rank"))
                agent = data.get("rank_name")
            else:
                mi = self.map_dict.get(match_map)
                mapImage = f"splash_{mi}_square".lower() if mi else None

            if not mapText:
                mapText = None
                mapImage = None

            return dict(
                state=S["in_party"].format(size=party_size, max=max_party),
                details=details,
                large_image=mapImage,
                large_text=mapText,
                small_image=agent_img,
                small_text=agent,
                start=int(self.start_time),
            )

        if session_state == "MENUS":
            is_idle = presence.get("isIdle")
            image = "game_icon_yellow" if is_idle else "game_icon"
            image_text = S["valorant_away"] if is_idle else S["valorant_online"]

            party_string = S["open_party"] if party_access == "OPEN" else S["closed_party"]

            gamemode = S["custom_game"] if party_state == "CUSTOM_GAME_SETUP" else self.gamemodes.get(presence.get("queueId"))

            return dict(
                state=f"{party_string} {S['count_suffix'].format(size=party_size, max=max_party)}",
                details=S["lobby_prefix"].format(gamemode=gamemode),
                large_image=image,
                large_text=image_text,
                small_image=str(data.get("rank")),
                small_text=data.get("rank_name"),
            )

        if session_state == "PREGAME":
            is_custom = presence.get("provisioningFlow") == "CustomGame" or party_state == "CUSTOM_GAME_SETUP"
            gamemode = S["custom_game"] if is_custom else self.gamemodes.get(presence.get("queueId"))

            match_map = (match_map or "").lower()
            mapText = self.map_dict.get(match_map)
            mapImage = f"splash_{mapText}_square".lower() if mapText else None

            return dict(
                state=S["in_party"].format(size=party_size, max=max_party),
                details=S["agent_select_prefix"].format(gamemode=gamemode),
                large_image=mapImage,
                large_text=mapText if mapText else None,
                small_image=str(data.get("rank")),
                small_text=data.get("rank_name"),
            )

        return None
