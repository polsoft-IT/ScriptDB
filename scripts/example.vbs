Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
WScript.Echo "Rozpoczynanie skryptu VBScript..."

' Wykonanie komendy
WshShell.Run "cmd /c echo Halo Świat", 1, True

' Zatrzymanie okna na końcu, aby nie zamykało się od razu
WScript.Echo "Skrypt VBScript zakończony. Kliknij OK, aby zamknąć."