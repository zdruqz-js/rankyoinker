' Hilfsdienst starten + Browser-Overlay oeffnen
' Doppelklick auf diese Datei statt auf vry.exe
'
' Startet AUSSCHLIESSLICH den Hilfsdienst (vry_log_server.py). vry.exe wird
' hier bewusst nicht angefasst — es haengt am VALORANT-Prozess: Der Dienst
' startet es von selbst, sobald das Spiel erreichbar ist, und schliesst es
' wieder mit. Ein Start von Hand wuerde vry.exe nur in "Connection error,
' retrying" haengen lassen, wenn VALORANT noch nicht laeuft.

Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = appDir

' Log-Server starten (beendet sich selbst, wenn er schon laeuft). Nutzt
' bewusst das mitgelieferte pythonw.exe per vollem Pfad statt eines
' systemweiten - RankYoinker braucht so keine separate Python-Installation.
shell.Run """" & appDir & "\pythonw.exe"" """ & appDir & "\vry_log_server.py""", 0, False

' Warten bis der Log-Server antwortet, dann die huebsche Adresse oeffnen.
' Klappt das nicht, faellt es auf die lokale Datei zurueck.
ready = False
For i = 1 To 12
    WScript.Sleep 500
    On Error Resume Next
    Set http = CreateObject("MSXML2.XMLHTTP")
    http.Open "GET", "http://127.0.0.1:1101/status", False
    http.Send
    If Err.Number = 0 Then
        If http.Status = 200 Then ready = True
    End If
    Err.Clear
    On Error GoTo 0
    If ready Then Exit For
Next

If ready Then
    ' Port 80 bevorzugen (dann reicht http://vry), sonst Port 1101
    okShort = False
    On Error Resume Next
    Set http2 = CreateObject("MSXML2.XMLHTTP")
    http2.Open "GET", "http://127.0.0.1/status", False
    http2.Send
    If Err.Number = 0 Then
        If http2.Status = 200 Then okShort = True
    End If
    Err.Clear
    On Error GoTo 0

    If okShort And fso.FileExists(fso.GetSpecialFolder(1) & "\drivers\etc\hosts") Then
        ' Alias nur nutzen, wenn er in der hosts-Datei eingetragen ist
        Set f = fso.OpenTextFile(fso.GetSpecialFolder(1) & "\drivers\etc\hosts", 1)
        hostsTxt = f.ReadAll
        f.Close
        If InStr(LCase(hostsTxt), "vry") > 0 Then
            shell.Run "http://vry/", 1, False
        Else
            shell.Run "http://localhost:1101/", 1, False
        End If
    Else
        shell.Run "http://localhost:1101/", 1, False
    End If
Else
    ' Kein Hilfsdienst erreichbar. Dann bleibt nur die Seite als Datei,
    ' ohne Logs, Skins, Shop und ohne Handy-Kopplung.
    shell.Run """" & appDir & "\index.html""", 1, False
End If
