import glob
import os
import time

from src.paths import data_dir

class Logging:
    # logFileOpened is a variable that keeps track of the log file status.
    # It is initialized as False to represent the log file wasn't open.
    def __init__(self):
        self.logFileOpened = False

    # `log_string` is a string that is the log message that will be written to the log file.
    def log(self, log_string: str):
        # Absoluter Pfad in %LOCALAPPDATA%\RankYoinker\logs statt relativ zum
        # Arbeitsverzeichnis in einem fuer alle Nutzer beschreibbaren
        # Programmordner (Sicherheitsfix 2026-09-28, siehe src/paths.py).
        logs_directory = os.path.join(data_dir(), "logs")

        if not os.path.exists(logs_directory):
            os.mkdir(logs_directory)

        log_files = glob.glob(os.path.join(logs_directory, "log-*.txt"))

        # The log file numbers are extracted from the filenames
        log_file_numbers = [int(os.path.basename(file)[4:-4]) for file in log_files]

        # If log_file_numbers list is empty, append the value 0 to it.
        # This ensures that the list always contains at least one value.
        if not log_file_numbers:
            log_file_numbers.append(0)

        log_file_name = os.path.join(
            logs_directory,
            f"log-{max(log_file_numbers) + 1 if not self.logFileOpened else max(log_file_numbers)}.txt")


        with open(log_file_name, "a" if self.logFileOpened else "w") as log_file:
            self.logFileOpened = True

            current_time = time.strftime("%Y.%m.%d-%H.%M.%S", time.localtime(time.time()))
            log_file.write(f"[{current_time}] {log_string.encode('ascii', 'replace').decode()}\n")