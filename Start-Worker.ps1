param(
    [string]$Server,
    [string]$PairCode,
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'AgentHarness\worker'),
    [int]$ConsolePort = 8787
)
$ErrorActionPreference = 'Stop'
$taskPython = if (Test-Path -LiteralPath 'D:\miniconda3\python.exe') { 'D:\miniconda3\python.exe' } else { (Get-Command python -ErrorAction Stop).Source }
$taskConfig = Join-Path $DataRoot 'pull-config.json'
if (-not (Test-Path -LiteralPath $taskConfig)) {
    if (-not $Server -or -not $PairCode) { throw '第一次运行需要 -Server 服务地址 -PairCode 配对代码' }
    & $taskPython -X utf8 (Join-Path $PSScriptRoot 'pull_worker.py') pair --server $Server --pair-code $PairCode --root $DataRoot
    if ($LASTEXITCODE -ne 0) { throw '配对失败' }
}
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
foreach ($taskScript in @('pull_worker.py','local_panel.py')) {
    $taskFullPath = Join-Path $PSScriptRoot $taskScript
    $taskExisting = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $taskPython -and $_.CommandLine -and $_.CommandLine.Contains($taskFullPath) -and $_.CommandLine.Contains($taskConfig) })
    if (-not $taskExisting.Count) {
        $taskArguments = @('-X','utf8',('"'+$taskFullPath+'"'),'--config',('"'+$taskConfig+'"'))
        if ($taskScript -eq 'local_panel.py') { $taskArguments += @('--port', [string]$ConsolePort) }
        Start-Process -FilePath $taskPython -ArgumentList $taskArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $DataRoot ($taskScript+'.stdout.log')) -RedirectStandardError (Join-Path $DataRoot ($taskScript+'.stderr.log')) | Out-Null
    }
}
Start-Process ('http://127.0.0.1:'+ $ConsolePort)
