param([switch]$Stop, [switch]$NoBrowser, [switch]$Shadow, [string]$Config = 'indexer/pipeline.rollout.json', [ValidateRange(1, 65535)][int]$Port = 8767)

$ErrorActionPreference = 'Stop'
$workerRoot = Split-Path -Parent $PSScriptRoot
$workerPython = Join-Path $workerRoot '.venv-indexer\Scripts\python.exe'
$workerData = Join-Path $PSScriptRoot 'data'
$workerStatePath = Join-Path $workerData $(if ($Port -eq 8767) { 'worker-launch.json' } else { 'worker-launch-' + $Port + '.json' })
$workerUrl = 'http://127.0.0.1:' + $Port
$workerStopPath = $workerStatePath + '.stop'
$workerDefaultDatabase = 'postgresql://spoilless@127.0.0.1:55439/spoilless_shadow'

try {
    Set-Location -LiteralPath $workerRoot
    $workerState = $null
    if (Test-Path -LiteralPath $workerStatePath) { $workerState = Get-Content -LiteralPath $workerStatePath -Raw | ConvertFrom-Json }
    if ($Stop) {
        if (-not $workerState -or $workerState.workspace -ne $workerRoot) { throw 'No launcher-owned worker was found. Stop a manually started worker in its PowerShell window with Ctrl+C.' }
        try { $workerHealth = Invoke-RestMethod -Uri ($workerUrl + '/health') -TimeoutSec 5 } catch {
            $supervisor = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $workerState.pid)
            if (-not $workerState.supervised -or -not $supervisor -or $supervisor.CommandLine -notlike '*pipeline.worker*--supervise*' -or -not $supervisor.CommandLine.Contains($workerState.config)) { throw 'No healthy launcher-owned worker or verified restarting supervisor was found.' }
            New-Item -ItemType File -Path $workerStopPath -Force | Out-Null
            Write-Host 'Worker restart cancelled. Saved checkpoints are retained.'
            exit 0
        }
        if ($workerHealth.workspace -ne $workerRoot -or ($workerHealth.pid -ne $workerState.pid -and $workerHealth.parent_pid -ne $workerState.pid -and $workerHealth.supervisor_pid -ne $workerState.pid -and $workerHealth.supervisor_parent_pid -ne $workerState.pid)) { throw 'This port belongs to a different worker. It was not stopped.' }
        Invoke-RestMethod -Method Post -Uri ($workerUrl + '/shutdown') -Headers @{Authorization=('Bearer ' + $workerState.token)} -TimeoutSec 5 | Out-Null
        Write-Host 'Worker shutdown requested. PostgreSQL remains running. Saved checkpoints are retained.'
        exit 0
    }
    if (-not (Test-Path -LiteralPath $workerPython)) { throw 'Worker Python environment is missing. Install indexer/requirements-worker.txt into .venv-indexer first.' }
    $workerConfig = (Resolve-Path -LiteralPath $Config).Path
    New-Item -ItemType Directory -Path $workerData -Force | Out-Null
    $env:PYTHONPATH = Join-Path $workerRoot 'indexer'
    if (-not $env:DATABASE_URL) { $env:DATABASE_URL = $workerDefaultDatabase }
    $env:SPOILLESS_PIPELINE_MODE = if ($Shadow) { 'shadow' } else { 'publish' }
    $env:SPOILLESS_STATUS_HOST = '127.0.0.1'
    $env:SPOILLESS_STATUS_PORT = [string]$Port
    $env:PYTHONUNBUFFERED = '1'
    if ($env:DATABASE_URL -eq $workerDefaultDatabase) {
        $workerPgCtl = Join-Path $workerData 'pg-test\bin\pg_ctl.exe'
        $workerCluster = Join-Path $workerData 'pg-test\cluster'
        if (-not (Test-Path -LiteralPath $workerPgCtl) -or -not (Test-Path -LiteralPath $workerCluster)) { throw 'Local PostgreSQL installation is missing. Configure DATABASE_URL for an existing PostgreSQL database.' }
        & $workerPgCtl status -D $workerCluster | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'Starting local PostgreSQL...'
            $workerPgLog = Join-Path $workerData 'pg-test\test.log'
            $workerPgProcess = Start-Process -FilePath $workerPgCtl -ArgumentList @('start', '-w', '-D', ('"' + $workerCluster + '"'), '-o', '"-h 127.0.0.1 -p 55439"', '-l', ('"' + $workerPgLog + '"')) -WindowStyle Hidden -Wait -PassThru
            if ($workerPgProcess.ExitCode -ne 0) { throw ('PostgreSQL could not start. See ' + $workerPgLog) }
        }
    }
    $workerHealth = $null
    try { $workerHealth = Invoke-RestMethod -Uri ($workerUrl + '/health') -TimeoutSec 2 } catch { $workerHealth = $null }
    if ($workerHealth) {
        if ($workerHealth.workspace -ne $workerRoot) { throw ('Port ' + $Port + ' is already in use. If this is the old worker, stop it with Ctrl+C and run Start Worker.cmd again.') }
        if ($workerHealth.state -eq 'stopping') { throw 'The worker is still stopping. Wait for shutdown to finish, then run Start Worker.cmd again.' }
        if (-not $workerState -or ($workerState.pid -ne $workerHealth.pid -and $workerState.pid -ne $workerHealth.parent_pid -and $workerState.pid -ne $workerHealth.supervisor_pid -and $workerState.pid -ne $workerHealth.supervisor_parent_pid) -or $workerState.workspace -ne $workerRoot) { throw 'A manually started worker is already running. Stop it with Ctrl+C once, then run Start Worker.cmd to manage it here.' }
        if ($workerHealth.mode -ne $env:SPOILLESS_PIPELINE_MODE -or $workerState.config -ne $workerConfig) { throw 'The worker is already running with different settings. Stop it before changing its configuration or mode.' }
        Invoke-RestMethod -Uri ($workerUrl + '/operator-status') -Headers @{Authorization=('Bearer ' + $workerState.token)} -TimeoutSec 10 | Out-Null
        if (-not $NoBrowser) { Start-Process ($workerUrl + '/#token=' + $workerState.token) }
        Write-Host ('Worker is already running in ' + $workerHealth.mode + ' mode. Status: ' + $workerUrl)
        exit 0
    }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw ('Port ' + $Port + ' is occupied but the worker is not healthy. Check the existing worker before starting another.') }
    if ($workerState -and $workerState.supervised -and $workerState.workspace -eq $workerRoot) {
        $supervisor = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $workerState.pid)
        if ($supervisor -and $supervisor.CommandLine -like '*pipeline.worker*--supervise*' -and $supervisor.CommandLine.Contains($workerState.config)) { throw 'The existing worker supervisor is restarting the worker. Do not launch a second instance; use Stop Worker first to cancel it.' }
    }
    & $workerPython -m pipeline.worker --config $workerConfig --check-config
    if ($LASTEXITCODE -ne 0) { throw 'Worker configuration validation failed.' }
    if (-not $env:SPOILLESS_STATUS_TOKEN) {
        $workerBytes = New-Object byte[] 32
        $workerRandom = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $workerRandom.GetBytes($workerBytes)
        $workerRandom.Dispose()
        $env:SPOILLESS_STATUS_TOKEN = ([BitConverter]::ToString($workerBytes)).Replace('-', '').ToLowerInvariant()
    }
    $workerLog = Join-Path $workerData ('worker-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Remove-Item -LiteralPath $workerStopPath -Force -ErrorAction SilentlyContinue
    $env:SPOILLESS_SUPERVISOR_STOP = $workerStopPath
    $workerProcess = Start-Process -FilePath $workerPython -ArgumentList @('-m', 'pipeline.worker', '--supervise', '--config', ('"' + $workerConfig + '"')) -WorkingDirectory $workerRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $workerLog -RedirectStandardError ($workerLog + '.error')
    @{workspace=$workerRoot; pid=$workerProcess.Id; supervised=$true; token=$env:SPOILLESS_STATUS_TOKEN; config=$workerConfig; log=$workerLog; mode=$env:SPOILLESS_PIPELINE_MODE} | ConvertTo-Json | Set-Content -LiteralPath $workerStatePath -Encoding UTF8
    Write-Host 'Starting worker...'
    for ($workerAttempt = 0; $workerAttempt -lt 120; $workerAttempt++) {
        $workerProcess.Refresh()
        if ($workerProcess.HasExited) { throw ('Worker exited during startup. See ' + $workerLog + '.error') }
        try { $workerHealth = Invoke-RestMethod -Uri ($workerUrl + '/health') -TimeoutSec 1 } catch { $workerHealth = $null }
        if ($workerHealth -and $workerHealth.workspace -eq $workerRoot -and ($workerHealth.pid -eq $workerProcess.Id -or $workerHealth.parent_pid -eq $workerProcess.Id -or $workerHealth.supervisor_pid -eq $workerProcess.Id -or $workerHealth.supervisor_parent_pid -eq $workerProcess.Id) -and $workerHealth.state -eq 'ready') {
            if (-not $NoBrowser) { Start-Process ($workerUrl + '/#token=' + $env:SPOILLESS_STATUS_TOKEN) }
            Write-Host ('Worker is running in ' + $env:SPOILLESS_PIPELINE_MODE + ' mode. Status: ' + $workerUrl)
            Write-Host ('Logs: ' + $workerLog + '.error')
            exit 0
        }
        Start-Sleep -Milliseconds 500
    }
    throw ('Worker has not become ready yet. It may still be starting. Check ' + $workerLog + '.error before launching it again.')
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
