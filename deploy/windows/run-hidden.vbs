' Launch run-lockstep.bat with no console window.
'
' Task Scheduler's own "Hidden" checkbox does not reliably suppress a console
' for a .bat, and a black window flashing up at every logon is exactly the kind
' of small annoyance that gets a system disabled. This wrapper is the standard
' way round it.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & here & "\run-lockstep.bat""", 0, False
