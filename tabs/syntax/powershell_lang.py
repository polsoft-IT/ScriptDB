# -*- coding: utf-8 -*-
"""PowerShell syntax module."""

from .base import SyntaxModule


class PowerShellModule(SyntaxModule):
    key = "powershell"
    label = "PowerShell"
    aliases = ("powershell", "pwsh", "ps1")
    extensions = (".ps1", ".psm1")
    # PowerShell uses "#" for comments, but "rem" also works and is common in
    # ports of batch scripts, so both markers are recognised.
    line_comment = "#"
    comments = {"rem": "rem", "batch": "#", "cmd": "#"}
    block_comments = (("<#", "#>"),)
    keywords = (
        "begin process end function filter param if elseif else switch "
        "foreach for while do until break continue return try catch finally "
        "throw trap class enum using in notlike like match replace split join "
        "where sort select"
    ).split()
    builtins = (
        "Write-Host Write-Output Write-Error Write-Warning Get-ChildItem "
        "Get-Content Set-Content Test-Path Join-Path Split-Path New-Item "
        "Remove-Item Copy-Item Move-Item Select-Object Where-Object "
        "ForEach-Object Measure-Object Sort-Object Get-Process Start-Process "
        "Stop-Process Get-Service Invoke-WebRequest Invoke-Expression "
        "ConvertTo-Json ConvertFrom-Json Start-Sleep Import-Module"
    ).split()
