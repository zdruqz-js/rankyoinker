; RankYoinker Installer (Inno Setup)
; ------------------------------------------------------------------
; Baut auf VALORANT Rank Yoinker (vRY) von Zay Kenyon and Contributors
; auf (https://github.com/zayKenyon/VALORANT-rank-yoinker, ISC-Lizenz).
;
; Dieses Skript packt die vRY-Dateien + die vorhandenen setup\*.py
; Skripte (Einrichtung/Deinstallation, laufen ueber das eingebettete
; Python - siehe Begruendung in install_all.py) in einen echten
; Windows-Installer, der:
;   1. alle Programmdateien nach %LocalAppData%\RankYoinker kopiert
;      (bewusst NICHT Program Files - die App schreibt zur Laufzeit
;      eigene Config-/Log-Dateien neben vry.exe, das braucht ohne
;      Admin-Rechte einen Ordner, in den der aktuelle Nutzer schreiben
;      darf),
;   2. danach automatisch setup\install_all.py ausführt (Start/Stop-
;      Buttons, Autostart des Log-Servers, http://vry + Firewall-
;      Freigabe fürs Handy - siehe LIESMICH.txt),
;   3. beim Deinstallieren setup\uninstall_all.py ausführt, damit
;      nichts (Autostart, Firewall-Regeln, hosts-Eintrag) zurückbleibt.
;
; WICHTIG: Dieses .iss-Skript kann nur AUF WINDOWS (oder mit Wine) zu
; einer echten Setup.exe kompiliert werden - Inno Setup selbst gibt es
; nicht für Linux. Kompilieren mit: iscc RankYoinker.iss
; ------------------------------------------------------------------

#define MyAppName "RankYoinker"
#define MyAppVersion "2.0.7-dev"
#define MyAppPublisher "RankYoinker"
#define MyAppURL "https://rankyoinker.de"
#define MyAppExeName "start_vry.vbs"

[Setup]
AppId={{8F1E9C3A-2B44-4E1A-9C7D-RANKYOINKER1}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; Der ganze Installer läuft erhoben (eine einzige UAC-Abfrage gleich zu
; Beginn). Ursprünglich lief nur der hosts-/Firewall-Schritt in
; install_all.py selbst erhoben (per "Start-Process -Verb RunAs -Wait"
; in der fruheren Batch-Version), aber genau dieses "-Wait" konnte in
; der Praxis hängen bleiben, ohne je zurückzukehren, obwohl der erhöhte
; Vorgang längst fertig war. Läuft der Installer von Anfang an als
; Admin, erkennt install_all.py das (per IsUserAnAdmin()) und führt den
; Schritt direkt aus, ganz ohne diese fehleranfällige verschachtelte
; Selbst-Erhebung.
PrivilegesRequired=admin
; {localappdata} ist bei einem erhöhten Installer nicht zuverlässig
; auflösbar (siehe Inno-Setup-Warnung "UsedUserAreasWarning") - könnte je
; nach Art der Erhebung auf den falschen Benutzerordner zeigen.
; {commonappdata} (ProgramData) ist eindeutig, unabhängig von Elevation.
DefaultDirName={commonappdata}\RankYoinker
DefaultGroupName=RankYoinker
DisableProgramGroupPage=yes
OutputBaseFilename=RankYoinker-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Passt sich automatisch an die Windows-Anzeigesprache an (LanguageDetectionMethod
; ist standardmässig "uilanguage") - "auto" zeigt die Sprachauswahl NUR, wenn die
; erkannte Systemsprache zu keiner der unten definierten Sprachen passt. Ohne das
; würde Inno Setup bei mehreren [Languages]-Einträgen immer einen Auswahldialog
; zeigen, egal ob Windows schon eindeutig Deutsch/Polnisch/... läuft.
ShowLanguageDialog=auto
UninstallDisplayIcon={app}\rankyoinker.ico
; Nachgebaut aus dem Logo oben links auf der Overlay-Seite selbst
; (.brand-mark in index.html: abgerundetes Quadrat, Gold-Verlauf, "Y").
; Erzeugt mit setup\make_icon.ps1.
SetupIconFile=rankyoinker.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung anlegen"; GroupDescription: "Zusätzliche Symbole:"

[Dirs]
; ProgramData-Unterordner sind für normale Nutzer standardmässig nur
; lesbar - die App schreibt zur Laufzeit aber eigene Dateien (config.json,
; logs\, vry_pair.json, ...) direkt neben vry.exe. "users-modify" gibt der
; eingeloggten Nutzergruppe Schreibrechte, ohne den ganzen ProgramData-Ordner
; für alle anderen Programme aufzuweichen.
Name: "{app}"; Permissions: users-modify

[Files]
; Programmdateien - alles ausser Logs/Cache/persönlichen Presets und
; Kopplungen des vorherigen Nutzers (die baut die App beim ersten Start
; von selbst neu auf, siehe LIESMICH.txt).
Source: "..\vry.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\vry_log_server.py"; DestDir: "{app}"; Flags: ignoreversion
; Eingebettetes Python 3.10.11 (offizielle "embeddable" Distribution von
; python.org) fuer den Log-Server (vry_log_server.py) - damit braucht
; RankYoinker keine systemweite Python-Installation mehr. "lib" (weiter
; unten) enthaelt die Drittanbieter-Pakete, python310._pth bindet den
; Ordner automatisch mit ein (siehe Kommentar in der Datei selbst).
Source: "..\python.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\pythonw.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\python310._pth"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\python310.zip"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\python.cat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\PYTHON-LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\*.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\*.pyd"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\index.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "..\LIESMICH.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\frozen_application_license.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\start_vry.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\start_vry_hidden.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\stop_vry.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\stop_vry_hidden.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "rankyoinker.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\lib\*"; DestDir: "{app}\lib"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "install_all.py"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "uninstall_all.py"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "open_overlay.bat"; DestDir: "{app}\setup"; Flags: ignoreversion
; Lizenzhinweis fürs Basiswerk (ISC-Pflichthinweis, siehe oben)
Source: "VRY-LICENSE.txt"; DestDir: "{app}"; DestName: "LICENSE-vRY.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\RankYoinker starten"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\rankyoinker.ico"
Name: "{group}\RankYoinker beenden"; Filename: "{app}\stop_vry.bat"; WorkingDir: "{app}"; IconFilename: "{app}\rankyoinker.ico"
Name: "{group}\Overlay im Browser öffnen"; Filename: "http://localhost:1101/"; IconFilename: "{app}\rankyoinker.ico"
Name: "{group}\{cm:UninstallProgram,RankYoinker}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\RankYoinker"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\rankyoinker.ico"; Tasks: desktopicon

[Run]
; Führt die vorhandene, bereits erprobte Einrichtung aus: Start/Stop-
; Buttons, Autostart, http://vry + Firewall (fragt bei Bedarf selbst
; nach Adminrechten, siehe install_all.py). Das Skript wartet nirgends
; auf einen Tastendruck - es läuft komplett durch und beendet sich von
; selbst, daher "runhidden": kein aufblitzendes Konsolenfenster mehr
; während die Assistenten-Fortschrittsanzeige (StatusMsg) läuft. Ein
; Fehlschlag ist trotzdem nachvollziehbar - install_all.py schreibt
; jeden Schritt mit Zeitstempel nach %TEMP%\rankyoinker_install_log.txt
; und zusätzlich nach {app}\rankyoinker-install-log.txt.
; install_all.py startet den Log-Server (und damit über kurz oder lang
; auch vry.exe, sobald VALORANT läuft) selbst, öffnet aber selbst KEINEN
; Browser mehr - das macht der zweite Eintrag unten (open_overlay.bat)
; erst, wenn der Nutzer die "Fertig"-Seite bestätigt. Sonst poppt der
; Browser mitten im Fortschrittsbalken auf, lange bevor der Assistent
; überhaupt fertig ist.
; python.exe statt pythonw.exe: laesst das Skript im (versteckten)
; Konsolenkontext laufen, statt stdout/stderr ins Leere gehen zu lassen -
; einfacher zu diagnostizieren, falls doch mal etwas schiefgeht.
Filename: "{app}\python.exe"; Parameters: """{app}\setup\install_all.py"""; \
    WorkingDir: "{app}\setup"; StatusMsg: "Richte Autostart, Firewall und Kurzadresse ein..."; Flags: waituntilterminated runhidden
; Öffnet die Overlay-Seite - "postinstall" heisst: läuft erst, wenn der
; Nutzer auf der "Fertig"-Seite auf "Fertigstellen" klickt, nicht vorher.
; "skipifsilent" hält eine stille/automatisierte Installation komplett
; browserfrei. Vorbelegt/angehakt, der Nutzer kann es auf der Fertig-
; Seite trotzdem abwählen.
Filename: "{cmd}"; Parameters: "/C ""{app}\setup\open_overlay.bat"""; \
    WorkingDir: "{app}\setup"; Description: "RankYoinker-Overlay jetzt im Browser öffnen"; \
    Flags: postinstall skipifsilent runhidden nowait

[UninstallRun]
; "runhidden" - siehe Begründung beim [Run]-Eintrag oben.
Filename: "{app}\python.exe"; Parameters: """{app}\setup\uninstall_all.py"""; \
    WorkingDir: "{app}\setup"; RunOnceId: "RankYoinkerCleanup"; Flags: waituntilterminated runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\__pycache__"
