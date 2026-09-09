"""RankYoinker - Einrichtung (Ersatz fuer das fruehere install_all.bat).

Warum Python statt Batch: am 08.09.2026 wurden in derselben install_all.bat
innerhalb weniger Stunden drei verschiedene, sehr subtile cmd.exe-Fallstricke
gefunden (ein "->" in einer Log-Nachricht, von cmd.exe als Umleitung
fehlinterpretiert; parallele Laeufe durch fehlendes Locking; ein
"if BEDINGUNG >Datei Befehl" ohne Klammern, das cmd.exe in einen haengenden
"More?"-Fortsetzungsmodus schickte). Batch-Quoting ist ein Minenfeld ohne
echtes try/except. Python mit subprocess-Argumentlisten (kein Shell-Parsing
mehr fuer netsh/reg) und echten Exceptions vermeidet diese ganze Klasse von
Fehlern. Das eingebettete Python (python.exe direkt neben diesem Skript)
macht das ohne zusaetzliche Laufzeit-Abhaengigkeit moeglich.

Macht dasselbe wie vorher:
  1. Start/Stop-Buttons der Website        (Registry HKCU, kein Admin)
  2. Autostart des Log-Servers              (Startordner, kein Admin)
  3. Kurze Adressen http://vry und http://rankyoinker + Firewall-Freigabe fuers Handy
                                             (hosts-Datei/Firewall, braucht Admin)
  4. Startet den Log-Server sofort
Rueckgaengig: uninstall_all.py

WICHTIG: Nur Schritt 3 braucht Adminrechte. Der Log-Server startet bewusst
OHNE Adminrechte - ein erhoehter Hintergrunddienst liesse sich sonst spaeter
nicht mehr normal beenden.

Die Firewall-Freigabe gilt NUR fuer das private Netzprofil und nur fuer
Adressen aus dem eigenen Subnetz. Sie oeffnet TCP 1101 (Log-Server, liefert
die Seite aus) und TCP 1100 (WebSocket von vry.exe). Ohne Kopplung ueber den
QR-Code kommt trotzdem kein fremdes Geraet an die Daten.
"""

import ctypes
import os
import subprocess
import sys
import time
import urllib.request
import winreg

APPDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUPDIR = os.path.join(APPDIR, "setup")
PYTHON_EXE = os.path.join(APPDIR, "python.exe")
PYTHONW_EXE = os.path.join(APPDIR, "pythonw.exe")
LOG_SERVER = os.path.join(APPDIR, "vry_log_server.py")

LOG_PATHS = [
    os.path.join(os.environ.get("TEMP", APPDIR), "rankyoinker_install_log.txt"),
    os.path.join(APPDIR, "rankyoinker-install-log.txt"),
]
LOCKFILE = os.path.join(APPDIR, ".install_all.lock")

FWLOG = "vRY Overlay - Log-Server 1101"
FWWS = "vRY Overlay - WebSocket 1100"
HOSTS_PATH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "etc", "hosts")
# "vry" bleibt aus Kompatibilitaetsgruenden (aeltere Lesezeichen/Anleitungen),
# "rankyoinker" ist der offizielle Produktname - beide zeigen auf 127.0.0.1.
HOSTS_NAMES = ["vry", "rankyoinker"]

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    for path in LOG_PATHS:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
    print(msg)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run(args, timeout=30):
    """subprocess mit Argument-LISTE - kein Shell-String-Parsing, also auch
    keine Quoting-Fallstricke fuer Regelnamen/Pfade mit Leerzeichen."""
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode
    except Exception as e:
        log("  FEHLER bei %r: %s: %s" % (args, type(e).__name__, e))
        return 1


def run_elevated_adminstep(timeout_sec=30):
    """Startet dieses Skript ein zweites Mal mit dem Argument 'adminstep',
    dieses Mal per UAC erhoeht, und wartet per echtem Prozess-Handle darauf.

    ShellExecuteEx (statt os.startfile/subprocess) ist noetig, weil nur das
    einen Handle liefert, auf den man mit WaitForSingleObject warten kann -
    genau das Problem, das frueher "Start-Process -Verb RunAs -Wait" in der
    Batch-Version manchmal unbegrenzt haengen liess. Mit einem Timeout auf
    dem Handle kann das hier nicht mehr passieren.
    """
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
        err = ctypes.GetLastError()
        log("  ShellExecuteEx fehlgeschlagen, GetLastError=%s" % err)
        return False

    if not sei.hProcess:
        log("  ShellExecuteEx lieferte keinen Prozess-Handle")
        return True  # kein Handle zum Warten -> optimistisch annehmen, dass es lief

    WAIT_TIMEOUT = 0x00000102
    result = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, timeout_sec * 1000)
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    if result == WAIT_TIMEOUT:
        log("  elevierter Vorgang hat das Zeitlimit (%ss) erreicht" % timeout_sec)
        return False
    return True


def kill_old_processes():
    log("Vorbereitung: alte vRY-Prozesse beenden")
    run([
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and "
        "$_.CommandLine -like '*vry_log_server.py*' } | ForEach-Object { "
        "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
    ])
    run(["taskkill", "/F", "/IM", "vry.exe", "/T"])
    log("Vorbereitung: fertig")


def step0_check_python():
    log("Schritt 0/4: Python-Check startet")
    ok = os.path.exists(PYTHONW_EXE)
    if not ok:
        log("  ACHTUNG: pythonw.exe fehlt im Programmordner - Installation unvollstaendig.")
    else:
        log("  pythonw.exe gefunden: %s" % PYTHONW_EXE)
    log("Schritt 0/4: fertig")
    return ok


def step1_registry_buttons():
    log("Schritt 1/4: Registry-Buttons startet")
    wscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
    start_vbs = os.path.join(APPDIR, "start_vry_hidden.vbs")
    stop_vbs = os.path.join(APPDIR, "stop_vry_hidden.vbs")

    def register(protocol, target_vbs):
        key_path = r"Software\Classes\%s" % protocol
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:vRY Protokoll (%s)" % protocol)
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        cmd_path = key_path + r"\shell\open\command"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_path) as k:
            command = '"%s" //B "%s"' % (wscript, target_vbs)
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)

    try:
        register("vry-start", start_vbs)
        register("vry-stop", stop_vbs)
        log("  Registry-Buttons erledigt.")
    except OSError as e:
        log("  FEHLER bei Registry-Buttons: %s" % e)
    log("Schritt 1/4: fertig")


def step2_autostart():
    log("Schritt 2/4: Autostart-Verknuepfung startet")
    startup_dir = os.path.join(
        os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
    )
    lnk = os.path.join(startup_dir, "vRY Log-Server.vbs")
    vbs_content = (
        "' Startet den vRY-Log-Server unsichtbar beim Anmelden.\r\n"
        'Set shell = CreateObject("WScript.Shell")\r\n'
        'shell.CurrentDirectory = "%s"\r\n'
        'shell.Run """%s"" ""%s""", 0, False\r\n' % (APPDIR, PYTHONW_EXE, LOG_SERVER)
    )
    try:
        os.makedirs(startup_dir, exist_ok=True)
        with open(lnk, "w", encoding="utf-8") as f:
            f.write(vbs_content)
        log("  Autostart-Verknuepfung geschrieben: %s" % lnk)
    except OSError as e:
        log("  FEHLER beim Schreiben der Autostart-Verknuepfung: %s" % e)
    log("Schritt 2/4: fertig")
    return lnk


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


def hosts_missing_entries():
    return [n for n in HOSTS_NAMES if not hosts_has_entry(n)]


def firewall_rule_exists(name):
    return run(["netsh", "advfirewall", "firewall", "show", "rule", "name=%s" % name]) == 0


def adminstep():
    """Laeuft erhoeht und fasst AUSSCHLIESSLICH hosts-Datei und Firewall an."""
    log("  adminstep gestartet")

    missing = hosts_missing_entries()
    if missing:
        try:
            with open(HOSTS_PATH, "a", encoding="utf-8") as f:
                for name in missing:
                    f.write("\n127.0.0.1 %s\n" % name)
            log("  adminstep - hosts-Eintraege geschrieben: %s" % ", ".join(missing))
            run(["ipconfig", "/flushdns"])
        except OSError as e:
            log("  adminstep - FEHLER beim Schreiben der hosts-Datei: %s" % e)

    run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=%s" % FWLOG])
    rc = run([
        "netsh", "advfirewall", "firewall", "add", "rule",
        "name=%s" % FWLOG, "dir=in", "action=allow", "protocol=TCP", "localport=1101",
        "profile=private", "remoteip=LocalSubnet",
        "description=vRY Overlay: Seite und API fuer Handys im eigenen Netz (nur gekoppelte Geraete)",
    ])
    log("  adminstep - Firewall-Regel 1101 rc=%s" % rc)

    run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=%s" % FWWS])
    rc = run([
        "netsh", "advfirewall", "firewall", "add", "rule",
        "name=%s" % FWWS, "dir=in", "action=allow", "protocol=TCP", "localport=1100",
        "profile=private", "remoteip=LocalSubnet",
        "description=vRY Overlay: WebSocket von vry.exe fuer Handys im eigenen Netz",
    ])
    log("  adminstep - Firewall-Regel 1100 rc=%s" % rc)
    log("  adminstep - fertig")


def step3_hosts_and_firewall():
    log("Schritt 3/4: hosts/Firewall-Check startet")
    need_admin = (
        bool(hosts_missing_entries())
        or not firewall_rule_exists(FWLOG)
        or not firewall_rule_exists(FWWS)
    )
    log("  Check fertig, need_admin=%s" % need_admin)

    no_admin = False
    no_fw = False

    if not need_admin:
        log("  war schon eingerichtet, ueberspringe Rest von Schritt 3")
    else:
        if is_admin():
            log("  bereits erhoeht, rufe adminstep direkt auf")
            adminstep()
        else:
            log("  NICHT erhoeht, starte elevierten Wiederaufruf (30s Zeitlimit)")
            print("      Windows fragt gleich nach Adminrechten - bitte bestaetigen.")
            print("      (Ablehnen ist ok - dann laeuft alles wie bisher nur am PC.)")
            ok = run_elevated_adminstep(30)
            if not ok:
                print("      UEBERSPRUNGEN - keine Adminrechte erhalten (oder Zeitlimit erreicht).")
                no_admin = True
                no_fw = True

        if not no_admin and not no_fw:
            if hosts_missing_entries():
                print("      Kurze Adresse: UEBERSPRUNGEN - Eintrag konnte nicht gesetzt werden.")
                no_admin = True
            else:
                print("      Kurze Adresse: eingetragen (http://vry/ und http://rankyoinker/).")
            if not firewall_rule_exists(FWLOG):
                print("      Firewall: FEHLGESCHLAGEN - Port 1101 ist nicht freigegeben.")
                no_fw = True
            elif not firewall_rule_exists(FWWS):
                print("      Firewall: FEHLGESCHLAGEN - Port 1100 ist nicht freigegeben.")
                no_fw = True
            else:
                print("      Firewall: Port 1100 und 1101 im privaten Netz freigegeben.")

    log("Schritt 3/4: fertig (no_admin=%s no_fw=%s)" % (no_admin, no_fw))
    return no_admin, no_fw


def wait_for_status(timeout_sec):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:1101/status", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def step4_start_server(lnk, no_py):
    log("Schritt 4/4: Log-Server starten")
    if no_py:
        print("      UEBERSPRUNGEN - Python fehlt.")
        log("Schritt 4/4: uebersprungen, no_py gesetzt")
        return

    def launch():
        try:
            subprocess.Popen(
                [PYTHONW_EXE, LOG_SERVER],
                cwd=APPDIR,
                creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                close_fds=True,
            )
        except Exception as e:
            log("  FEHLER beim Starten des Log-Servers: %s: %s" % (type(e).__name__, e))

    launch()
    log("  Log-Server gestartet, warte auf /status (1. Versuch)...")
    if wait_for_status(6):
        log("  laeuft.")
        print("      laeuft.")
    else:
        log("  laeuft noch nicht, zweiter Versuch...")
        print("      laeuft noch nicht, versuche es noch einmal...")
        launch()
        if wait_for_status(8):
            log("  laeuft (2. Versuch).")
            print("      laeuft (2. Versuch).")
        else:
            log("  laeuft immer noch nicht.")
            print("      laeuft immer noch nicht - einmal start_vry.vbs ausfuehren.")
    log("Schritt 4/4: fertig")


def acquire_lock():
    """Atomare Sperrdatei per exklusivem Datei-Erstellen ('x') - kein
    Zeitfenster zwischen Pruefen und Schreiben wie bei der frueheren
    Batch-Version (dort verursachte genau das mehrere parallele Laeufe,
    die sich minutenlang gegenseitig Firewall-Regeln kaputt gemacht haben).
    """
    try:
        with open(LOCKFILE, "x", encoding="utf-8") as f:
            f.write("%s pid=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
        return True
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(LOCKFILE)
        except OSError:
            age = 999
        if age < 180:
            return False
        # Verwaiste Sperrdatei (z.B. nach einem Absturz) - ignorieren und uebernehmen.
        try:
            os.remove(LOCKFILE)
            with open(LOCKFILE, "x", encoding="utf-8") as f:
                f.write("%s pid=%s (nach verwaister Sperre)\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
        except OSError:
            pass
        return True


def release_lock():
    try:
        os.remove(LOCKFILE)
    except OSError:
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "adminstep":
        log("Sonderlauf 'adminstep' erkannt (elevierter Wiederaufruf)")
        adminstep()
        return 0

    log("=== install_all.py gestartet (Args: %s) ===" % sys.argv[1:])
    log("  APPDIR=%s" % APPDIR)

    if not acquire_lock():
        log("  Sperrdatei ist aktuell - ein anderer Lauf laeuft vermutlich noch, beende mich sofort.")
        return 0

    try:
        kill_old_processes()

        if not os.path.exists(LOG_SERVER):
            log("FEHLER: vry_log_server.py nicht gefunden in %s" % APPDIR)
            print("FEHLER: vry_log_server.py wurde nicht gefunden.")
            print("Gesucht in: %s" % APPDIR)
            return 1

        print("=" * 60)
        print("  vRY Overlay - Einrichtung")
        print("  Ordner: %s" % APPDIR)
        print("=" * 60)
        print()

        no_py = not step0_check_python()
        step1_registry_buttons()
        lnk = step2_autostart()
        no_admin, no_fw = step3_hosts_and_firewall()
        step4_start_server(lnk, no_py)

        print()
        print("=" * 60)
        print("  FERTIG")
        print("=" * 60)
        if no_admin:
            print("  Seite oeffnen mit:   http://localhost:1101")
        else:
            print("  Seite oeffnen mit:   http://rankyoinker/  (oder http://vry/, http://localhost:1101)")
        if no_fw:
            print("  Handy im selben WLAN: NICHT eingerichtet (Firewall fehlt).")
        else:
            print("  Handy im selben WLAN: Handy-Symbol auf der Seite -> QR-Code scannen.")
        print("  vRY selbst startest du ueber den Button auf der Seite")
        print("  oder mit start_vry.vbs im Hauptordner.")
        print()

        log("=== install_all.py fertig, exit 0 ===")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
