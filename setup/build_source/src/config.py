import json
from io import TextIOWrapper
from json import JSONDecodeError
import requests
import os
import time

from src.constants import DEFAULT_CONFIG
from src.paths import config_path
from src import valapi

def apply_defaults(cls):
    for name, value in DEFAULT_CONFIG.items():
        setattr(cls, name, value)
    return cls

@apply_defaults
class Config:
    def __init__(self, log):
        self.log = log
        # Absoluter Pfad statt der frueheren bloss-relativen "config.json"
        # (haengte vom Arbeitsverzeichnis zum Aufrufzeitpunkt ab - siehe
        # rpc.py._config_path()-Kommentar fuer den Bug, den das schon mal
        # verursacht hat) UND jetzt in %LOCALAPPDATA%\RankYoinker statt neben
        # der exe (Sicherheitsfix 2026-09-28, siehe src/paths.py).
        cfg_path = config_path()

        if not os.path.exists(cfg_path):
            self.log("config.json not found, creating new one")
            try:
                with open(cfg_path, "w") as file:
                    config = self.config_dialog(file)
            except OSError as e:
                self.log(f"could not create config.json ({e}) - using defaults for this run")

        config = {}
        try:
            # utf-8-sig: von Hand gespeichert (Editor, PowerShell) oft mit BOM - bis 2.5.7 scheiterte
            # das Lesen dann und die ganze Datei wurde auf die Standardwerte zurueckgesetzt
            # (Update-Kanal, Sprache, Spiel-Overlay, Performance-Modus weg)
            with open(cfg_path, "r", encoding="utf-8-sig") as file:
                self.log("config opened")
                config = json.load(file)
            if not isinstance(config, dict):
                raise ValueError("config.json is not an object")

            keys = [k for k in config.keys()] # getting the keys in the file
            default_keys = [k for k in DEFAULT_CONFIG.keys()] # getting the keys in the self.default
            missingkeys = list(filter(lambda x: x not in keys, default_keys)) # comparing the keys in the file to the keys in the default and returning the missing keys

            if len(missingkeys) > 0:
                self.log("config.json is missing keys")
                self.log(f"missing keys: " + str(missingkeys))
                for key in missingkeys:
                    config[key] = DEFAULT_CONFIG[key]
                self._save_missing(cfg_path, config)

        except ValueError:      # JSONDecodeError, UnicodeDecodeError
            # Nicht mehr ueberschreiben - nur fuer diesen Lauf mit Standardwerten arbeiten
            self.log("invalid file - using defaults for this run, config.json left untouched")
            config = {}
        except OSError as e:
            # z. B. von einem Virenscanner gesperrt - lieber ohne gespeicherte Einstellungen starten als gar nicht
            self.log(f"could not read config.json ({e}) - using defaults for this run")
            config = {}
        finally:
            config = DEFAULT_CONFIG | config
            for name, value in config.items():
                setattr(self, name, value)

            self.log(f"config class dict: {self.__dict__}")

            self.log(f"got cooldown with value '{self.cooldown}'")

            if not self.weapon_check(config["weapon"]):
                self.weapon = "vandal" # if the user manually entered a wrong name into the config file, this will be the default until changed by the user.
            else:   
                self.weapon = config["weapon"]
            
    def _save_missing(self, cfg_path, config):
        """Fehlende Schluessel nachtragen - darf den Start NIE verhindern (die Standardwerte gelten
        fuer diesen Lauf ohnehin). Bis 2.5.9 stand os.replace noch INNERHALB des with-open zum Lesen:
        Windows verweigert das Ersetzen einer Datei, die (ohne FILE_SHARE_DELETE) offen ist -
        PermissionError [WinError 5], vry.exe stuerzte bei jedem Start ab. Betraf jede config.json,
        die der Dienst zuerst angelegt hatte (nur Sprache/Kanal/Overlay - typisch bei neuen Installationen)."""
        # ueber eine Zwischendatei: der Dienst liest config.json gleichzeitig und haette eine
        # halb geschriebene Datei als kaputt beiseitegelegt
        tmp = cfg_path + ".tmp"
        try:
            with open(tmp, 'w', encoding="utf-8") as w:
                json.dump(config, w, indent=4)
            for attempt in range(5):
                try:
                    os.replace(tmp, cfg_path)
                    self.log("Succesfully added missing keys")
                    return
                except PermissionError:
                    # liest gerade der Dienst oder ein Virenscanner die Datei? kurz warten
                    time.sleep(0.2)
            self.log("config.json is locked - missing keys not saved, using defaults for this run")
        except OSError as e:
            self.log(f"could not save missing keys ({e}) - using defaults for this run")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

    def get_feature_flag(self,key):
        return self.__dict__.get("flags",DEFAULT_CONFIG["flags"]).get(key,DEFAULT_CONFIG["flags"][key])

    def get_table_flag(self,key):
        return self.__dict__.get("table",DEFAULT_CONFIG["flags"]).get(key,DEFAULT_CONFIG["table"][key])         

    def config_dialog(self, fileToWrite: TextIOWrapper):
        self.log("color config prompt called")
        jsonToWrite = DEFAULT_CONFIG
        
        json.dump(jsonToWrite, fileToWrite, indent=4)
        return jsonToWrite

    def weapon_check(self, name):
        if name in [weapon["displayName"] for weapon in valapi.get("https://valorant-api.com/v1/weapons").json()["data"]]:
            return True
        else:
            return False
