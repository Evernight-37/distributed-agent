$ErrorActionPreference = 'Stop'
$taskPython = (Get-Command python -ErrorAction Stop).Source
Start-Process -FilePath $taskPython -ArgumentList @('-X', 'utf8', ('"' + (Join-Path $PSScriptRoot 'panel.py') + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
Start-Process 'http://127.0.0.1:8765'
