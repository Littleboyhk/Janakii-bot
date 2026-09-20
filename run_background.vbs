Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "c:\Users\hk270\Downloads\Janakii"
WshShell.Run "python bot.py", 0, False
