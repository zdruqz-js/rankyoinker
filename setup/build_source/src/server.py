import ipaddress
import json
import logging
import os
import re
import threading
import time

from websocket_server import WebsocketServer
from websocket_server.websocket_server import WebSocketHandler

from src.constants import version
from src.paths import config_path, data_dir

logging.getLogger('websocket_server.websocket_server').disabled = True

# Sicherheit (2.5.3, Pruefung vom 2026-10-17): Der WebSocket lauschte bis 2.5.2 auf
# 0.0.0.0 ohne jede Pruefung - jede im Browser offene Webseite (ws://localhost:1100)
# und jedes Geraet im selben WLAN konnte Spielerdaten und den Ingame-Chat live
# mitlesen, die Handy-Kopplung war damit umgangen. Jetzt:
#  - nur noch 127.0.0.1,
#  - Handshake nur von unserer eigenen Seite (Origin) bzw. ohne Origin (kein Browser),
#  - Host nur unsere Namen oder eine IP (gegen DNS-Rebinding).
# Das Handy holt dieselben Daten ueber den Dienst (/api/live, mit Kopplung) - dafuer
# liegen Version + letzter Heartbeat zusaetzlich in DATA_DIR/live.json.
_ORIGIN_OK = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\]|vry|rankyoinker)(:\d+)?$", re.I)
_HOST_NAMES_OK = {"localhost", "127.0.0.1", "[::1]", "vry", "rankyoinker"}
LIVE_FILE = os.path.join(data_dir(), "live.json")


def _origin_ok(origin):
    return not origin or bool(_ORIGIN_OK.match(origin))


def _host_ok(host):
    host = (host or "").strip().lower()
    if not host:
        return True
    if host.startswith("["):
        name = host.split("]", 1)[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name in _HOST_NAMES_OK:
        return True
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True
    except ValueError:
        return False


class _GuardedHandler(WebSocketHandler):
    """Wie WebSocketHandler.handshake() aus websocket_server 0.6.4, aber mit
    Origin-/Host-Pruefung vor der Antwort (Version in requirements.txt gepinnt)."""

    def handshake(self):
        try:
            headers = self.read_http_headers()
        except Exception:
            self.keep_alive = False
            return
        if not _origin_ok(headers.get("origin")) or not _host_ok(headers.get("host")):
            self.keep_alive = False
            return
        if (headers.get("upgrade") or "").lower() != "websocket" or not headers.get("sec-websocket-key"):
            self.keep_alive = False
            return
        response = self.make_handshake_response(headers["sec-websocket-key"])
        with self._send_lock:
            self.handshake_done = self.request.send(response.encode())
        self.valid_client = True
        self.server._new_client_(self)


class _GuardedServer(WebsocketServer):
    def __init__(self, host, port):
        super().__init__(host=host, port=port)
        self.RequestHandlerClass = _GuardedHandler


# websocket.enableTrace(True)

class Server:
    def __init__(self, log, Error):
        self.Error = Error
        self.log = log
        self.lastMessages = {}
        # seq startet bei der Uhrzeit: nach einem Neustart von vry.exe nie dieselbe Nummer wie vorher
        self._live = {"seq": int(time.time() * 1000), "version": None, "heartbeat": None}
        self._live_lock = threading.Lock()

    def start_server(self):
        try:
            # print(self.lastMessage)
            with open(config_path(), "r") as conf:
                port = json.load(conf)["port"]
            self.server = _GuardedServer(host="127.0.0.1", port=port)
            # server = websocket.WebSocketApp("wss://localhost:1100", on_open=on_open, on_message=on_message, on_close=on_close)
            self.server.set_fn_new_client(self.handle_new_client)
            self.server.run_forever(threaded=True)
            # frischer Stand fuers Handy: Heartbeat der letzten Sitzung gilt nicht mehr
            self._live["heartbeat"] = None
            self._write_live("version", {"core": version, "type": "version"})
        except Exception as e:
            self.Error.PortError(port)

    def handle_new_client(self, client, server):
        self.send_payload("version",{
            "core": version
        })
        for key in self.lastMessages:
            if key not in ["chat","version"]:
                self.send_message(self.lastMessages[key])

    def send_message(self, message):
        self.server.send_message_to_all(message)

    def send_payload(self, type, payload):
        payload["type"] = type
        msg_str = json.dumps(payload)
        self.lastMessages[type] = msg_str
        self.server.send_message_to_all(msg_str)
        if type in ("version", "heartbeat"):
            self._write_live(type, payload)

    def _write_live(self, type, payload):
        """Version + letzter Heartbeat fuers Handy (Dienst: /api/live). Chat nie."""
        try:
            with self._live_lock:
                self._live[type] = payload
                self._live["seq"] += 1
                self._live["ts"] = time.time()
                tmp = LIVE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._live, f)
                os.replace(tmp, LIVE_FILE)
        except Exception:
            pass
