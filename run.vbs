' run.vbs — Launch GrammarAnywhere without a console window.
' Prefers the portable Python bundled by the installer, then the py
' launcher (pyw ignores venvs that hijack PATH), then whatever
' pythonw is on PATH.

Set fso   = CreateObject("Scripting.FileSystemObject")
appDir    = fso.GetParentFolderName(WScript.ScriptFullName)
embedded  = appDir & "\python\pythonw.exe"

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = appDir

If fso.FileExists(embedded) Then
    shell.Run """" & embedded & """ -m spell_daemon", 0, False
Else
    On Error Resume Next
    shell.Run "pyw.exe -3 -m spell_daemon", 0, False
    If Err.Number <> 0 Then
        Err.Clear
        shell.Run "pythonw.exe -m spell_daemon", 0, False
    End If
    On Error GoTo 0
End If
