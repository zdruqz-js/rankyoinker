"""RankYoinker - Entfernen (Ersatz fuer das fruehere uninstall_all.bat).

Siehe install_all.py fuer die Begruendung des Wechsels von Batch auf Python.

Macht rueckgaengig, was install_all.py eingerichtet hat:
  1. beendet vry.exe und den Log-Server
  2. entfernt die Start/Stop-Buttons (Registry, HKCU)
  3. entfernt den Autostart                (Startordner)
  4. entfernt die hosts-Eintraege "vry"/"rankyoinker" und die Firewall-Freigaben (braucht Admin)
Die Programmdateien selbst bleiben liegen.

WICHTIG: Nur Schritt 4 laeuft erhoeht - genau wie bei install_all.py. Die
Schritte 2 und 3 MUESSEN unter dem angemeldeten Benutzer laufen: HKCU und
der Autostart-Ordner gehoeren dem Benutzer, nicht dem Administrator. Ein
komplett erhoehtes Skript raeumt sonst beim falschen Benutzer auf und meldet
trotzdem Erfolg.
"""

import ctypes
import os
import subprocess
import sys
import time
import winreg

APPDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUPDIR = os.path.join(APPDIR, "setup")
PYTHON_EXE = os.path.join(APPDIR, "python.exe")

FWLOG = "vRY Overlay - Log-Server 1101"
FWWS = "vRY Overlay - WebSocket 1100"
HOSTS_PATH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "etc", "hosts")
HOSTS_NAMES = ["vry", "rankyoinker"]

CREATE_NO_WINDOW = 0x08000000


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return r.returncode
    except Exception:
        return 1


def run_elevated_adminstep(timeout_sec=30):
    """Siehe install_all.py:run_elevated_adminstep - identisches Vorgehen
    per ShellExecuteEx + WaitForSingleObject mit Zeitlimit."""
    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.c_void_p),
            ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p),
            ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p),
            ("hKeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_ulong),
            ("hIconOrMonitor", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0

    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = PYTHON_EXE
    sei.lpParameters = '"%s" adminstep' % os.path.abspath(__file__)
    sei.lpDirectory = SETUPDIR
    sei.nShow = SW_HIDE

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        return False
    if not sei.hProcess:
        return True

    WAIT_TIMEOUT = 0x00000102
    result = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, timeout_sec * 1000)
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return result != WAIT_TIMEOUT


def hosts_has_entry(name):
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0] == "127.0.0.1" and parts[1].lower() == name.lower():
                    return True
    except OSError:
        pass
    return False


def hosts_has_any_entry():
    return any(hosts_has_entry(n) for n in HOSTS_NAMES)


def firewall_rule_exists(name):
    return run(["netsh", "advfirewall", "firewall", "show", "rule", "name=%s" % name]) == 0


def remove_hosts_entries():
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return False
    names_lower = {n.lower() for n in HOSTS_NAMES}
    kept = [
        l for l in lines
        if not (len(l.split()) >= 2 and l.split()[0] == "127.0.0.1" and l.split()[1].lower() in names_lower)
    ]
    joined = "".join(kept)
    # Sicherheitsnetz: eine (fast) leere Datei NIE zurueckschreiben - lieber
    # die Eintraege stehen lassen als die hosts-Datei des Systems leerraeumen.
    if len(joined.strip()) == 0:
        return False
    try:
        with open(HOSTS_PATH + ".vry-backup", "w", encoding="utf-8", errors="ignore") as f:
            f.write("".join(lines))
        with open(HOSTS_PATH, "w", encoding="utf-8", errors="ignore") as f:
            f.write(joined)
        run(["ipconfig", "/flushdns"])
        return True
    except OSError:
        return False


def adminstep():
    run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=%s" % FWLOG])
    run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=%s" % FWWS])
    if hosts_has_any_entry():
        remove_hosts_entries()


def remove_registry_buttons():
    print("[2/4] Entferne die Start/Stop-Buttons...")
    rest = False
    for name in ("vry-start", "vry-stop"):
        try:
            _delete_key_tree(winreg.HKEY_CURRENT_USER, r"Software\Classes\%s" % name)
        except OSError:
            pass
    for name in ("vry-start", "vry-stop"):
        try:
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\%s" % name)
            rest = True
        except OSError:
            pass
    if rest:
        print("      FEHLGESCHLAGEN - die Eintraege sind noch da.")
    else:
        print("      erledigt.")
    return rest


def _delete_key_tree(root, path):
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        return
    while True:
        try:
            sub = winreg.EnumKey(key, 0)
        except OSError:
            break
        _delete_key_tree(root, path + "\\" + sub)
    key.Close()
    winreg.DeleteKey(root, path)


def remove_autostart():
    print("[3/4] Entferne den Autostart...")
    lnk = os.path.join(
        os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
        "vRY Log-Server.vbs",
    )
    if not os.path.exists(lnk):
        print("      war nicht eingerichtet.")
        return False
    try:
        os.remove(lnk)
        print("      erledigt.")
        return False
    except OSError:
        print("      FEHLGESCHLAGEN - Datei laesst sich nicht loeschen: %s" % lnk)
        return True


def remove_hosts_and_firewall():
    print('[4/4] Entferne die hosts-Eintraege "vry"/"rankyoinker" und die Firewall-Freigaben...')
    need_admin = hosts_has_any_entry() or firewall_rule_exists(FWLOG) or firewall_rule_exists(FWWS)
    if not need_admin:
        print("      war nicht eingerichtet.")
        return False

    admin_fail = False
    if is_admin():
        adminstep()
    else:
        print("      Windows fragt gleich nach Adminrechten - bitte bestaetigen.")
        print("      (Ablehnen ist ok - dann bleiben nur diese Eintraege stehen.)")
        if not run_elevated_adminstep(30):
            print("      UEBERSPRUNGEN - keine Adminrechte erhalten (oder Zeitlimit erreicht).")
            admin_fail = True

    if admin_fail:
        return True

    rest = False
    if hosts_has_any_entry():
        print("      Kurze Adresse: FEHLGESCHLAGEN - ein Eintrag steht noch.")
        rest = True
    else:
        print("      Kurze Adresse: entfernt.")
    if firewall_rule_exists(FWLOG):
        print("      Firewall: FEHLGESCHLAGEN - Regel fuer Port 1101 steht noch.")
        rest = True
    elif firewall_rule_exists(FWWS):
        print("      Firewall: FEHLGESCHLAGEN - Regel fuer Port 1100 steht noch.")
        rest = True
    else:
        print("      Firewall: Freigaben entfernt.")
    return rest


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "adminstep":
        adminstep()
        return 0

    print("=" * 60)
    print("  vRY Overlay - Entfernen")
    print("  Ordner: %s" % APPDIR)
    print("=" * 60)
    print()

    if is_admin():
        print("  HINWEIS: Dieses Fenster laeuft als Administrator.")
        print('  Bitte NICHT "als Administrator ausfuehren" - einfach doppelklicken.')
        print("  Sonst werden die Einstellungen des Administrators aufgeraeumt")
        print("  statt deiner eigenen. Nach Admin-Rechten wird bei Bedarf gefragt.")
        print()

    rest = False

    print("[1/4] Beende laufende Prozesse...")
    run(["taskkill", "/F", "/T", "/IM", "vry.exe"])
    run([
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and "
        "$_.CommandLine -like '*vry_log_server*' } | ForEach-Object { "
        "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
    ])
    print("      erledigt.")
    print()

    rest = remove_registry_buttons() or rest
    print()
    rest = remove_autostart() or rest
    print()
    rest = remove_hosts_and_firewall() or rest

    print()
    print("=" * 60)
    if rest:
        print("  TEILWEISE ERLEDIGT - siehe die Meldungen oben.")
        print()
        print("  Nicht entfernte Reste von Hand:")
        print("    Buttons:   regedit > HKEY_CURRENT_USER\\Software\\Classes")
        print("               dort vry-start und vry-stop loeschen")
        print("    Autostart: Win+R  >  shell:startup  >  \"vRY Log-Server.vbs\"")
        print("    Adresse:   %s" % HOSTS_PATH)
        print('               Zeilen "127.0.0.1 vry" und "127.0.0.1 rankyoinker" entfernen (als Admin)')
        print('    Firewall:  wf.msc > Eingehende Regeln > "vRY Overlay - ..." loeschen')
    else:
        print("  FERTIG - es laeuft nichts mehr im Hintergrund.")
        print("  Die Programmdateien kannst du jetzt einfach loeschen.")
    print()
    print('  Gekoppelte Handys stehen in "%s\\vry_pair.json".' % APPDIR)
    print("  Datei loeschen = alle Geraete sind entkoppelt.")
    print("=" * 60)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
