; RankYoinker Installer (Inno Setup)
; ------------------------------------------------------------------
; Baut auf VALORANT Rank Yoinker (vRY) von Zay Kenyon and Contributors
; auf (https://github.com/zayKenyon/VALORANT-rank-yoinker, ISC-Lizenz).
;
; Dieses Skript packt die vRY-Dateien + die vorhandenen setup\*.py
; Skripte (Einrichtung/Deinstallation, laufen ueber das eingebettete
; Python - siehe Begruendung in install_all.py) in einen echten
; Windows-Installer, der:
;   1. alle Programmdateien nach C:\Program Files\RankYoinker kopiert
;      (bis 2.4.1 ProgramData, siehe DefaultDirName unten; Laufzeitdaten
;      liegen seit 2.3.14 ohnehin in %LOCALAPPDATA%\RankYoinker),
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
#define MyAppVersion "2.4.9-dev2"
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
; Seit 2.4.2 Program Files statt ProgramData (Nutzerfrage 2026-10-12):
; In Unterordnern von C:\ProgramData dürfen normale Benutzer laut Windows-
; Standardrechten NEUE Dateien anlegen. Weil der Uninstaller python.exe aus
; {app} mit Adminrechten startet, hätte ein Standardkonto dort vorher z. B.
; eine DLL ablegen können (Rechteausweitung). Program Files erlaubt das nicht.
; Die App schreibt seit 2.3.14 nichts mehr in {app}, es braucht also keine
; Schreibrechte dort. Alte Installationen in ProgramData zieht [Code] unten
; beim Update um; ein selbst gewählter anderer Ordner bleibt erhalten.
; 64-Bit-Modus, damit {autopf} "C:\Program Files" ist und nicht "(x86)" -
; das eingebettete Python ist ohnehin amd64.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DefaultDirName={code:GetInstallDir}
UsePreviousAppDir=no
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
; SICHERHEITSFIX (2026-09-28, siehe Community-Sicherheitsbericht): {app} hatte
; hier vorher "Permissions: users-modify" - jede lokal angemeldete Person
; (nicht nur die installierende) konnte damit Dateien in diesem Ordner
; ersetzen, u.a. vry_log_server.py, python.exe und die *.dll/*.pyd-Dateien
; selbst. Kombiniert mit [UninstallRun] unten (fuehrt python.exe aus GENAU
; diesem Ordner mit Adminrechten aus, da PrivilegesRequired=admin) war das
; eine echte Rechteausweitung auf Mehrnutzer-Maschinen: ein gewoehnliches
; Standardkonto haette dort Code platzieren koennen, der beim naechsten
; admin-elevierten Deinstallieren durch irgendjemand anderen ausgefuehrt
; worden waere.
; {app} hat jetzt Standardrechte (seit 2.4.2 Program Files: nur
; Administratoren schreibend, alle lesend) und enthaelt NUR NOCH Programmdateien. Alles, was
; die App zur Laufzeit selbst schreibt (config.json, Presets, Kopplung,
; Logs, der heruntergeladene Update-Installer, ...) liegt seitdem in
; %LOCALAPPDATA%\RankYoinker (siehe src/paths.py bzw. DATA_DIR in
; vry_log_server.py) - das ist bereits nutzerspezifisch beschreibbar, ganz
; ohne eine Berechtigung hier vergeben zu muessen, die sich falsch
; konfigurieren liesse. Bestehende Installationen ziehen ihre Daten beim
; ersten Start nach dem Update automatisch dorthin um (_migrate_legacy_data()
; in vry_log_server.py).
Name: "{app}"

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
; config.json wird seit dem Sicherheitsfix 2026-09-28 nicht mehr hierher
; kopiert - es lebt jetzt in %LOCALAPPDATA%\RankYoinker (DATA_DIR, siehe
; [Dirs] oben) und wird von Config.__init__() (src/config.py) beim ersten
; Start automatisch mit denselben Standardwerten angelegt, die vorher aus
; config.default.json kamen (DEFAULT_CONFIG in src/constants.py - inhaltlich
; identisch, nur eine Python- statt JSON-Quelle).
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
; Logs UND die transienten Update-Dateien (heruntergeladener Installer, Warte-
; Skript, Update-Log) leben seit dem Sicherheitsfix 2026-09-28 in
; %LOCALAPPDATA%\RankYoinker (DATA_DIR) statt in {app} - wie die Zeilen oben
; bewusst nur diese, keine persoenlichen Daten (config.json, Presets,
; Kopplung): die sollen ein Uninstall/Reinstall weiterhin ueberleben, genau
; wie es fuer {app} schon vorher der Fall war.
Type: filesandordirs; Name: "{localappdata}\RankYoinker\logs"
Type: files; Name: "{localappdata}\RankYoinker\rankyoinker-update.exe"
Type: files; Name: "{localappdata}\RankYoinker\rankyoinker-update-log.txt"
Type: files; Name: "{localappdata}\RankYoinker\rankyoinker-update-wait.bat"
; Plattencache der valorant-api.com-Kataloge (ab 2.4.5, src/valapi.py) - rund
; 10 MB, wird beim naechsten Start einfach neu geholt.
Type: filesandordirs; Name: "{localappdata}\RankYoinker\cache"

[Code]
// ---- Umzug ProgramData -> Program Files (2.4.2) ----
// Bis 2.4.1 lag RankYoinker in C:\ProgramData\RankYoinker und wurde im
// 32-Bit-Modus installiert (Deinstallations-Eintrag in der 32-Bit-Ansicht der
// Registry). Beim ersten Update auf 2.4.2+:
//   1. vor dem Kopieren die laufenden RankYoinker-Prozesse aus dem alten
//      Ordner beenden,
//   2. nach der Installation den alten Ordner leeren - bis auf Laufzeitdaten
//      von Installationen vor 2.3.14, die der Dienst beim ersten Start noch
//      nach %LOCALAPPDATA% kopiert (_migrate_legacy_data),
//   3. den alten 32-Bit-Deinstallations-Eintrag löschen, sonst stünde
//      RankYoinker zweimal unter "Apps & Features".
const
  // Ja, zwei schließende Klammern: AppId oben ist {{...}} - nur die vordere
  // Doppelklammer ist ein Escape, die hintere steht wörtlich im Schlüssel.
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F1E9C3A-2B44-4E1A-9C7D-RANKYOINKER1}}_is1';
  LegacyData = '|.rankyoinker_client_id|.rankyoinker_feature_counts.json|.rankyoinker_last_report.json|.rankyoinker_matches_tracked_seen.json|config.json|vry_presets.json|vry_encounters.json|vry_instalock.json|vry_pair.json|vry_lol.json|vry_lol_presets.json|vry_lol_encounters.json|';

var
  PrevDir: String;

function OldDefaultDir(): String;
begin
  Result := ExpandConstant('{commonappdata}\RankYoinker');
end;

function SameDir(A, B: String): Boolean;
begin
  Result := CompareText(RemoveBackslashUnlessRoot(A), RemoveBackslashUnlessRoot(B)) = 0;
end;

// Das Batch-Warteskript der App bis 2.4.5-dev5 hat nie auf das Ende des alten
// Dienstes gewartet und startet ~3 s nach diesem Installer den ALTEN Dienst
// neu ("Sicherheitsnetz") - mitten in die Installation. Lief der Dienst
// erhoeht (kein UAC-Fenster), brach /VERYSILENT dann lautlos ab, weil
// python310.dll & Co. in Benutzung waren (Live-Analyse 2026-10-13). Das
// Warteskript kommt immer von der ALTEN Version, darum raeumt der neue
// Installer selbst auf: nur cmd.exe beenden, NICHT den Prozessbaum - dieser
// Installer ist ein Kind davon.
procedure StopOldUpdateWaiter();
var
  RC: Integer;
begin
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq ''cmd.exe'' -and $_.CommandLine -like ''*rankyoinker-update-wait.bat*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
    '', SW_HIDE, ewWaitUntilTerminated, RC);
end;

function InitializeSetup(): Boolean;
begin
  StopOldUpdateWaiter();
  PrevDir := '';
  if not RegQueryStringValue(HKLM64, UninstKey, 'Inno Setup: App Path', PrevDir) then
    if not RegQueryStringValue(HKLM32, UninstKey, 'Inno Setup: App Path', PrevDir) then
      PrevDir := '';
  Result := True;
end;

// Ziel: ein bewusst anders gewählter Ordner bleibt, der alte ProgramData-
// Standard wird durch Program Files ersetzt.
function GetInstallDir(Param: String): String;
begin
  if (PrevDir <> '') and (not SameDir(PrevDir, OldDefaultDir())) and DirExists(PrevDir) then
    Result := PrevDir
  else
    Result := ExpandConstant('{autopf}\RankYoinker');
end;

function MigratingFromProgramData(): Boolean;
begin
  Result := (PrevDir <> '') and SameDir(PrevDir, OldDefaultDir())
    and (not SameDir(ExpandConstant('{app}'), OldDefaultDir())) and DirExists(OldDefaultDir());
end;

// Beendet den Dienst (python/pythonw mit vry_log_server.py, egal woher) UND
// jeden Prozess, der das eingebettete Python aus {app} benutzt - der haelt
// python310.dll/*.pyd offen, auch ohne vry_log_server.py in der Kommandozeile
// (so geschehen 2026-10-13: ein Hilfsskript mit {app}\python.exe liess zwei
// stille Updates scheitern). Beim Umzug aus ProgramData laeuft der Dienst noch
// aus dem alten Ordner - den deckt die Kommandozeilen-Pruefung ab.
procedure StopOldProcesses();
var
  RC: Integer;
  AppDir: String;
begin
  AppDir := ExpandConstant('{app}');
  StringChangeEx(AppDir, '''', '''''', True);   // ' -> '' fuer PowerShell
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -Command "$app = ''' + AppDir + '\''; Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq ''pythonw.exe'' -or $_.Name -eq ''python.exe'') -and ($_.CommandLine -like ''*vry_log_server.py*'' -or ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($app, [System.StringComparison]::OrdinalIgnoreCase))) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
    '', SW_HIDE, ewWaitUntilTerminated, RC);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM vry.exe /T', '', SW_HIDE, ewWaitUntilTerminated, RC);
end;

procedure CleanOldDir(Dir: String);
var
  FR: TFindRec;
begin
  if FindFirst(Dir + '\*', FR) then
  begin
    try
      repeat
        if (FR.Name <> '.') and (FR.Name <> '..') and (Pos('|' + Lowercase(FR.Name) + '|', LegacyData) = 0) then
        begin
          if (FR.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
            DelTree(Dir + '\' + FR.Name, True, True, True)
          else
            DeleteFile(Dir + '\' + FR.Name);
        end;
      until not FindNext(FR);
    finally
      FindClose(FR);
    end;
  end;
  RemoveDir(Dir);   // klappt nur, wenn keine Altdaten mehr drinliegen
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  // Vor dem Kopieren IMMER laufende Dienste und vry.exe beenden (vorher nur
  // beim Umzug aus ProgramData): sonst sind python310.dll, *.pyd und
  // library.zip in Benutzung, und eine stille Installation bricht ab.
  // install_all.py startet den Dienst danach neu.
  if CurStep = ssInstall then
    StopOldProcesses();
  if CurStep = ssPostInstall then
  begin
    if MigratingFromProgramData() then
      CleanOldDir(OldDefaultDir());
    if RegKeyExists(HKLM32, UninstKey) then
      RegDeleteKeyIncludingSubkeys(HKLM32, UninstKey);
  end;
end;
