$ErrorActionPreference = 'Stop'
$taskPython = if (Test-Path -LiteralPath 'D:\miniconda3\python.exe') { 'D:\miniconda3\python.exe' } else { (Get-Command python -ErrorAction Stop).Source }
$taskPanelPath = Join-Path $PSScriptRoot 'panel.py'
$taskState = Join-Path (Split-Path $PSScriptRoot -Parent) 'agent-harness-data'
if ($env:AGENT_HARNESS_STATE) { $taskState = $env:AGENT_HARNESS_STATE }
$taskListeners = @(Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue)
if ($taskListeners.Count) {
    $taskExisting = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskListeners[0].OwningProcess)"
    if (-not $taskExisting.CommandLine.Contains($taskPanelPath)) { throw 'Port 8765 is occupied by another program' }
} else {
    New-Item -ItemType Directory -Force -Path $taskState | Out-Null
    $taskArguments = @('-X', 'utf8', ('"' + $taskPanelPath + '"'), '--state', ('"' + $taskState + '"'))
    $taskProcess = Start-Process -FilePath $taskPython -ArgumentList $taskArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskState 'panel.stdout.log') -RedirectStandardError (Join-Path $taskState 'panel.stderr.log') -PassThru
    @{pid=$taskProcess.Id;source=$taskPanelPath} | ConvertTo-Json | Set-Content (Join-Path $taskState 'panel-process.json') -Encoding utf8
}
Start-Process 'http://127.0.0.1:8765'
