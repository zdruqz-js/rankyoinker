import json
from io import TextIOWrapper
from json import JSONDecodeError
import requests
import os

from src.constants import DEFAULT_CONFIG
from src.paths import config_path

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
            with open(cfg_path, "w") as file:
                config = self.config_dialog(file)

        try:
            with open(cfg_path, "r") as file:
                self.log("config opened")
                config = json.load(file)

                keys = [k for k in config.keys()] # getting the keys in the file
                default_keys = [k for k in DEFAULT_CONFIG.keys()] # getting the keys in the self.default
                missingkeys = list(filter(lambda x: x not in keys, default_keys)) # comparing the keys in the file to the keys in the default and returning the missing keys

                if len(missingkeys) > 0:
                    self.log("config.json is missing keys")
                    with open(cfg_path, 'w') as w:
                        self.log(f"missing keys: " + str(missingkeys))
                        for key in missingkeys:
                            config[key] = DEFAULT_CONFIG[key]

                        self.log("Succesfully added missing keys")
                        json.dump(config, w, indent=4)

        except (JSONDecodeError):
            self.log("invalid file")
            with open(cfg_path, "w") as file:
                config = self.config_dialog(file)
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
        if name in [weapon["displayName"] for weapon in requests.get("https://valorant-api.com/v1/weapons").json()["data"]]:
            return True
        else:
            return False
