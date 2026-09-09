' Wird vom Browser-Button (vry-start://) aufgerufen: startet den Hilfsdienst
' unsichtbar, ohne Browser zu oeffnen.
'
' Startet AUSSCHLIESSLICH den Hilfsdienst (vry_log_server.py). vry.exe wird
' hier bewusst nicht angefasst — der Dienst startet es selbst, sobald
' VALORANT erreichbar ist, und schliesst es wieder mit.

Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = appDir

' Log-Server starten (beendet sich selbst, wenn er schon laeuft). Nutzt
' bewusst das mitgelieferte pythonw.exe per vollem Pfad statt eines
' systemweiten - RankYoinker braucht so keine separate Python-Installation.
shell.Run """" & appDir & "\pythonw.exe"" """ & appDir & "\vry_log_server.py""", 0, False

' Kurz warten, bis er antwortet — mehr ist hier nicht zu tun
For i = 1 To 12
    WScript.Sleep 500
    On Error Resume Next
    Set http = CreateObject("MSXML2.XMLHTTP")
    http.Open "GET", "http://127.0.0.1:1101/status", False
    http.Send
    ok = (Err.Number = 0)
    If ok Then ok = (http.Status = 200)
    Err.Clear
    On Error GoTo 0
    If ok Then Exit For
Next
