' run.vbs — Launch GrammarAnywhere without a console window.
' Interpreter preference (mirrors run_spell.bat):
'   1. portable Python bundled by the installer (python\ next to this script)
'   2. Python 3.12 via the py launcher — the prebuilt GPU llama-cpp-python
'     wheel is a cp312 build, so local GGUF models need 3.12
'   3. the py launcher's default Python (cloud / llama-server modes work fine)
'   4. whatever pythonw is on PATH (last resort; PATH can be hijacked by venvs)

Set fso   = CreateObject("Scripting.FileSystemObject")
appDir    = fso.GetParentFolderName(WScript.ScriptFullName)
embedded  = appDir & "\python\pythonw.exe"

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = appDir

If fso.FileExists(embedded) Then
    shell.Run """" & embedded & """ -m spell_daemon", 0, False
Else
    launched = False
    On Error Resume Next
    ' Probe with py.exe (hidden, wait for exit code) before launching pyw.exe,
    ' because Run with bWaitOnReturn=False cannot report a bad -3.12 version.
    rc = shell.Run("py.exe -3.12 -V", 0, True)
    If Err.Number = 0 And rc = 0 Then
        shell.Run "pyw.exe -3.12 -m spell_daemon", 0, False
        launched = True
    Else
        Err.Clear
        rc = shell.Run("py.exe -3 -V", 0, True)
        If Err.Number = 0 And rc = 0 Then
            shell.Run "pyw.exe -3 -m spell_daemon", 0, False
            launched = True
        End If
    End If
    Err.Clear
    On Error GoTo 0
    If Not launched Then
        shell.Run "pythonw.exe -m spell_daemon", 0, False
    End If
End If
