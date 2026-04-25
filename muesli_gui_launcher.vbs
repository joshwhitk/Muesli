Dim fso, shell, root, pythonExe, guiScript, bootstrapScript, splashScript, statusPath, tracePath, cmd, splashCmd, token, i, textFile, powershellExe

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Randomize

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonExe = root & "\.venv\Scripts\python.exe"
guiScript = root & "\muesli_gui.py"
bootstrapScript = root & "\muesli_gui_bootstrap.py"
splashScript = root & "\muesli_splash.ps1"
token = CStr(Fix(Timer * 1000)) & "-" & CStr(Int((Rnd * 900000) + 100000))
statusPath = root & "\.launch_status_" & token & ".json"
tracePath = root & "\.launch_trace_" & token & ".log"
powershellExe = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
If fso.FileExists(bootstrapScript) Then
    cmd = Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & bootstrapScript & Chr(34) & " --launch-token " & Chr(34) & token & Chr(34)
Else
    cmd = Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & guiScript & Chr(34) & " --launch-token " & Chr(34) & token & Chr(34)
End If

Sub AppendTrace(eventName, detailText)
    Dim traceFile
    Set traceFile = fso.OpenTextFile(tracePath, 8, True)
    traceFile.WriteLine CStr(Now) & vbTab & eventName & vbTab & detailText
    traceFile.Close
End Sub

Call AppendTrace("launcher_started", "GUI wrapper launched from shell.")

For i = 0 To WScript.Arguments.Count - 1
    cmd = cmd & " " & Chr(34) & WScript.Arguments(i) & Chr(34)
Next

Set textFile = fso.CreateTextFile(statusPath, True)
textFile.WriteLine "{""stage"":""Starting Muesli..."",""progress"":5,""close"":false,""detail"":""Starting Python and importing Muesli. This can take a bit on cold launch.""}"
textFile.Close
Call AppendTrace("status_written", "Initial status file created before splash/Python launch.")

If fso.FileExists(splashScript) Then
    splashCmd = Chr(34) & powershellExe & Chr(34) & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " _
        & Chr(34) & splashScript & Chr(34) & " -Root " & Chr(34) & root & Chr(34) _
        & " -Token " & Chr(34) & token & Chr(34)
    Call AppendTrace("splash_launch_requested", splashCmd)
    shell.Run splashCmd, 0, False
    Call AppendTrace("splash_launch_spawned", "PowerShell splash process was requested.")
End If

Call AppendTrace("python_launch_requested", cmd)
shell.Run cmd, 0, False
Call AppendTrace("python_launch_spawned", "Python GUI bootstrap process was requested.")
