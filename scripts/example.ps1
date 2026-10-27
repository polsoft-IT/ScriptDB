Write-Host "Rozpoczynanie skryptu PowerShell..."

# Uzyskanie listy procesów
Get-Process | Select-Object Name, ID | Out-GridView

# Uruchomienie polecenia systemu
Start-Sleep -Seconds 2
Write-Host "Skrypt PowerShell zakończony."