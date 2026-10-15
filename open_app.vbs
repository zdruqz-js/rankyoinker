' Startmenue- und Desktop-Symbol "RankYoinker" (ab 2.5.0): oeffnet die
' Oberflaeche als eigenes Fenster (Edge/Chrome im App-Modus) statt als
' Browser-Tab. Laeuft der Hilfsdienst nicht, startet ihn vry_log_server.py
' --open-app vorher selbst. Die eigentliche Logik steckt dort
' (open_overlay_window), hier nur der unsichtbare Aufruf.

Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = appDir
shell.Run """" & appDir & "\pythonw.exe"" """ & appDir & "\vry_log_server.py"" --open-app", 0, False
