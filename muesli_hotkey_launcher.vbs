Dim fso, shell, root, pythonExe, hotkeyScript, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonExe = root & "\.venv\Scripts\python.exe"
hotkeyScript = root & "\muesli_hotkey.py"
cmd = Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & hotkeyScript & Chr(34)

shell.Run cmd, 0, False
