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
#define MyAppVersion "2.2.4"
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
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

; Die eingebauten [Languages]-MessagesFile-Dateien uebersetzen nur die
; Standard-Inno-Setup-UI (Weiter/Zurueck/Lizenzseite/Fertig-Seite, ...).
; Alles, was RankYoinker selbst an eigenem Text in den Installer schreibt
; (Task-Checkbox, Verknuepfungsnamen, Fortschrittstext, Checkbox auf der
; Fertig-Seite), muss dafuer extra hier uebersetzt werden - sonst waere
; das fuer jede gewaehlte Sprache ausser Deutsch ein Bruch mitten im
; sonst durchgehend lokalisierten Assistenten.
[CustomMessages]
english.DesktopIconDesc=Create a desktop icon
german.DesktopIconDesc=Desktop-Verknüpfung anlegen
polish.DesktopIconDesc=Utwórz ikonę na pulpicie
french.DesktopIconDesc=Créer une icône sur le bureau
spanish.DesktopIconDesc=Crear un icono en el escritorio
turkish.DesktopIconDesc=Masaüstü simgesi oluştur
korean.DesktopIconDesc=바탕화면에 바로가기 만들기

english.AdditionalIconsGroup=Additional icons:
german.AdditionalIconsGroup=Zusätzliche Symbole:
polish.AdditionalIconsGroup=Dodatkowe ikony:
french.AdditionalIconsGroup=Icônes supplémentaires :
spanish.AdditionalIconsGroup=Iconos adicionales:
turkish.AdditionalIconsGroup=Ek simgeler:
korean.AdditionalIconsGroup=추가 아이콘:

english.StartMenuStartName=Start RankYoinker
german.StartMenuStartName=RankYoinker starten
polish.StartMenuStartName=Uruchom RankYoinker
french.StartMenuStartName=Démarrer RankYoinker
spanish.StartMenuStartName=Iniciar RankYoinker
turkish.StartMenuStartName=RankYoinker'ı başlat
korean.StartMenuStartName=RankYoinker 시작

english.StartMenuStopName=Stop RankYoinker
german.StartMenuStopName=RankYoinker beenden
polish.StartMenuStopName=Zatrzymaj RankYoinker
french.StartMenuStopName=Arrêter RankYoinker
spanish.StartMenuStopName=Detener RankYoinker
turkish.StartMenuStopName=RankYoinker'ı durdur
korean.StartMenuStopName=RankYoinker 종료

english.StartMenuOverlayName=Open overlay in browser
german.StartMenuOverlayName=Overlay im Browser öffnen
polish.StartMenuOverlayName=Otwórz nakładkę w przeglądarce
french.StartMenuOverlayName=Ouvrir l'overlay dans le navigateur
spanish.StartMenuOverlayName=Abrir el overlay en el navegador
turkish.StartMenuOverlayName=Overlay'i tarayıcıda aç
korean.StartMenuOverlayName=브라우저에서 오버레이 열기

english.InstallStatusMsg=Setting up autostart, firewall and short address...
german.InstallStatusMsg=Richte Autostart, Firewall und Kurzadresse ein...
polish.InstallStatusMsg=Konfigurowanie autostartu, zapory i krótkiego adresu...
french.InstallStatusMsg=Configuration du démarrage automatique, du pare-feu et de l'adresse courte...
spanish.InstallStatusMsg=Configurando el inicio automático, el firewall y la dirección corta...
turkish.InstallStatusMsg=Otomatik başlatma, güvenlik duvarı ve kısa adres ayarlanıyor...
korean.InstallStatusMsg=자동 시작, 방화벽, 단축 주소를 설정하는 중...

english.OpenOverlayNowDesc=Open the RankYoinker overlay in the browser now
german.OpenOverlayNowDesc=RankYoinker-Overlay jetzt im Browser öffnen
polish.OpenOverlayNowDesc=Otwórz teraz nakładkę RankYoinker w przeglądarce
french.OpenOverlayNowDesc=Ouvrir l'overlay RankYoinker dans le navigateur maintenant
spanish.OpenOverlayNowDesc=Abrir ahora el overlay de RankYoinker en el navegador
turkish.OpenOverlayNowDesc=RankYoinker overlay'ini şimdi tarayıcıda aç
korean.OpenOverlayNowDesc=지금 브라우저에서 RankYoinker 오버레이 열기

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIconDesc}"; GroupDescription: "{cm:AdditionalIconsGroup}"

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
; Eine .json pro Sprache (de.json, pl.json, ...) statt fest in index.html
; eingebettet - siehe /api/languages + /lang/<code>.json in vry_log_server.py.
; Neue Sprache = neue Datei hier reinlegen, kein Code-Update noetig.
Source: "..\lang\*"; DestDir: "{app}\lang"; Flags: ignoreversion recursesubdirs createallsubdirs
; config.default.json ist im Repo versioniert (das *Standard*-Template) -
; "..\config.json" waere die tatsaechliche Laufzeit-Konfiguration dieser
; Maschine (siehe .gitignore) und existiert deshalb in einem frischen CI-
; Checkout gar nicht.
Source: "config.default.json"; DestDir: "{app}"; DestName: "config.json"; Flags: onlyifdoesntexist
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
Name: "{group}\{cm:StartMenuStartName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\rankyoinker.ico"
Name: "{group}\{cm:StartMenuStopName}"; Filename: "{app}\stop_vry.bat"; WorkingDir: "{app}"; IconFilename: "{app}\rankyoinker.ico"
Name: "{group}\{cm:StartMenuOverlayName}"; Filename: "http://localhost:1101/"; IconFilename: "{app}\rankyoinker.ico"
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
    WorkingDir: "{app}\setup"; StatusMsg: "{cm:InstallStatusMsg}"; Flags: waituntilterminated runhidden
; Öffnet die Overlay-Seite - "postinstall" heisst: läuft erst, wenn der
; Nutzer auf der "Fertig"-Seite auf "Fertigstellen" klickt, nicht vorher.
; "skipifsilent" hält eine stille/automatisierte Installation komplett
; browserfrei. Vorbelegt/angehakt, der Nutzer kann es auf der Fertig-
; Seite trotzdem abwählen.
Filename: "{cmd}"; Parameters: "/C ""{app}\setup\open_overlay.bat"""; \
    WorkingDir: "{app}\setup"; Description: "{cm:OpenOverlayNowDesc}"; \
    Flags: postinstall skipifsilent runhidden nowait

[UninstallRun]
; "runhidden" - siehe Begründung beim [Run]-Eintrag oben.
Filename: "{app}\python.exe"; Parameters: """{app}\setup\uninstall_all.py"""; \
    WorkingDir: "{app}\setup"; RunOnceId: "RankYoinkerCleanup"; Flags: waituntilterminated runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\__pycache__"
