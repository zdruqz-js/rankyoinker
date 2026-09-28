import os
import sys


def base_dir() -> str:
    """Ordner, in dem die PROGRAMMDATEIEN liegen (vry.exe/pythonw.exe, DLLs,
    lib/, index.html, ...) - im gefrorenen vry.exe direkt neben der exe
    selbst (sys.executable), beim Testen aus dem Quellcode eine Ebene ueber
    src/ (Projekt-Root). Seit dem Sicherheitsfix vom 2026-09-28 (siehe
    data_dir() unten) fuer normale Nutzer nur noch LESBAR - hier duerfen
    keine Laufzeitdaten mehr hineingeschrieben werden."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    """Vom Programmordner getrennter, garantiert vom aktuell eingeloggten
    Nutzer OHNE Sonderrechte beschreibbarer Ordner fuer alles, was zur
    Laufzeit entsteht (config.json, Presets, Kopplung, Logs, ...).

    Vorher lag all das direkt neben vry.exe in ProgramData, mit expliziten
    Schreibrechten fuer JEDE lokal angemeldete Person ("Permissions:
    users-modify" in RankYoinker.iss) - kombiniert mit dem admin-elevierten
    Uninstaller, der python.exe/vry_log_server.py genau aus diesem Ordner
    ausfuehrt, war das eine echte Rechteausweitung: auf einer Mehrnutzer-
    Maschine konnte JEDE Person mit einem gewoehnlichen Standardkonto dort
    Code platzieren, der beim naechsten (admin-elevierten) Deinstallieren
    durch irgendjemand anderen mit Adminrechten ausgefuehrt worden waere
    (siehe Community-Sicherheitsbericht, 2026-09-28).

    %LOCALAPPDATA% ist bereits nutzerspezifisch und braucht dafuer keine
    Sonderrechte-Vergabe, die man falsch konfigurieren koennte - genau das
    richtige Werkzeug. Bestehende Dateien aus dem alten Ort werden einmalig
    herueberkopiert, siehe migrate_legacy_data() in vry_log_server.py."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "RankYoinker")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")
