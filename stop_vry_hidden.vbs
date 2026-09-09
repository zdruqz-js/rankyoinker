' Wird vom Browser-Button (vry-stop://) aufgerufen: beendet vRY ohne Fenster.
' Beendet ALLE vry.exe-Instanzen (auch doppelte/haengende) samt Kindprozessen.
' Der Log-Server laeuft bewusst WEITER - er liefert die Overlay-Seite aus
' (http://localhost:1101 bzw. http://vry). Zum kompletten Beenden: stop_vry.bat
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = appDir

' Alle vry.exe hart beenden (/T = inkl. Kindprozesse), synchron warten
shell.Run "taskkill /F /T /IM vry.exe", 0, True
