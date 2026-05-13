Dim fso, shell, root, pythonExe, pythonwExe, selectedPython, hotkeyScript, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
' Same off-Dropbox-first venv resolution as muesli_gui_launcher.vbs.
Dim envVenv, defaultVenv, legacyVenv, venvRoot
envVenv = shell.ExpandEnvironmentStrings("%MUESLI_VENV%")
defaultVenv = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\muesli\.venv")
legacyVenv = root & "\.venv"
If envVenv <> "%MUESLI_VENV%" And envVenv <> "" And fso.FolderExists(envVenv) Then
    venvRoot = envVenv
ElseIf fso.FolderExists(defaultVenv) Then
    venvRoot = defaultVenv
Else
    venvRoot = legacyVenv
End If
' Prefer pythonw.exe (no console window) over python.exe so the user
' doesn't see a black CLI window flash up at boot when the Startup
' shortcut fires this script. Falls back to python.exe only if pythonw
' is missing for some reason. Same precedence the GUI launcher uses.
pythonExe = venvRoot & "\Scripts\python.exe"
pythonwExe = venvRoot & "\Scripts\pythonw.exe"
selectedPython = pythonExe
If fso.FileExists(pythonwExe) Then
    selectedPython = pythonwExe
End If
hotkeyScript = root & "\muesli_hotkey.py"
cmd = Chr(34) & selectedPython & Chr(34) & " " & Chr(34) & hotkeyScript & Chr(34)

shell.Run cmd, 0, False
