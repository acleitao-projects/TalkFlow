' Launches TalkFlow with no console window (pythonw.exe, not python.exe), so
' it behaves like a normal background/tray app when started from a shortcut
' or from the Windows Startup folder instead of a terminal.
Set shell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
projectDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(scriptDir)
pythonw = projectDir & "\.venv\Scripts\pythonw.exe"
shell.CurrentDirectory = projectDir
shell.Run """" & pythonw & """ -m talkflow.app", 0, False
