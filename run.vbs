' run.vbs — Launch GrammarAnywhere without a console window.
' Checks for a portable Python bundled by the installer before
' falling back to whatever system Python is on PATH.

Set fso   = CreateObject("Scripting.FileSystemObject")
appDir    = fso.GetParentFolderName(WScript.ScriptFullName)
embedded  = appDir & "\python\pythonw.exe"

If fso.FileExists(embedded) Then
    pythonExe = embedded
Else
    pythonExe = "pythonw.exe"
End If

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = appDir
shell.Run """" & pythonExe & """ -m spell_daemon", 0, False
