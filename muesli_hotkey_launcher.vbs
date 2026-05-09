Dim fso, shell, root, pythonExe, hotkeyScript, cmd

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
pythonExe = venvRoot & "\Scripts\python.exe"
hotkeyScript = root & "\muesli_hotkey.py"
cmd = Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & hotkeyScript & Chr(34)

shell.Run cmd, 0, False
