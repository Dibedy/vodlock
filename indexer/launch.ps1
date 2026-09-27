param([switch]$Stop, [switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$studioRoot = $PSScriptRoot
$studioServer = Join-Path $studioRoot 'server.py'
$studioPython = Join-Path $studioRoot '.venv\Scripts\python.exe'
$studioData = Join-Path $studioRoot 'data'
$studioPidFile = Join-Path $studioData 'server.pid'
$studioUrl = 'http://127.0.0.1:8766'

try {
    if ($Stop) {
        $studioState = Invoke-RestMethod -Uri ($studioUrl + '/api/state') -TimeoutSec 2
        if ($studioState.workspace -ne $studioRoot -or -not $studioState.token) {
            throw 'This port belongs to a different app or Round Studio folder. It was not stopped.'
        }
        Invoke-RestMethod -Method Post -Uri ($studioUrl + '/api/shutdown') -Headers @{'X-VODLOCK-Token'=$studioState.token} -ContentType 'application/json' -Body '{}' | Out-Null
        if (Test-Path -LiteralPath $studioPidFile) { Remove-Item -LiteralPath $studioPidFile }
        Write-Host 'Round Studio is stopping. Any active index will cancel and clean up its decoder first.'
        exit 0
    }

    try {
        $studioState = Invoke-RestMethod -Uri ($studioUrl + '/api/state') -TimeoutSec 2
    } catch {
        $studioState = $null
    }
    if ($studioState.token -and $null -ne $studioState.jobs) {
        if ($studioState.workspace -ne $studioRoot) {
            throw ('Round Studio already runs from ' + $studioState.workspace + '. Stop that instance before starting this folder.')
        }
        if (-not $NoBrowser) { Start-Process $studioUrl }
        exit 0
    }

    if (-not (Test-Path -LiteralPath $studioPython)) {
        $studioCandidates = @()
        $studioPyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($studioPyCommand) {
            foreach ($studioVersion in @('-3.12', '-3.11', '-3.10')) {
                try {
                    $studioCandidates += (& $studioPyCommand.Source $studioVersion -c 'import sys; print(sys.executable)' 2>$null)
                } catch {}
            }
        }
        $studioPythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($studioPythonCommand) { $studioCandidates += $studioPythonCommand.Source }
        $studioBundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $studioBundledPython) { $studioCandidates += $studioBundledPython }
        $studioBasePython = $null
        foreach ($studioCandidate in $studioCandidates) {
            if (-not $studioCandidate -or -not (Test-Path -LiteralPath $studioCandidate)) { continue }
            & $studioCandidate -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { $studioBasePython = $studioCandidate; break }
        }
        if (-not $studioBasePython) { throw 'Install Python 3.12 from python.org, then run this launcher again.' }
        Write-Host 'Setting up Round Studio in its own local environment. This may take a few minutes.'
        & $studioBasePython -m venv (Join-Path $studioRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python environment.' }
    }
    & $studioPython -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(name) for name in ('rapidocr_onnxruntime', 'imageio_ffmpeg', 'yt_dlp')) else 1)"
    if ($LASTEXITCODE -ne 0) {
        & $studioPython -m pip install -r (Join-Path $studioRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your connection and run the launcher again.' }
    }
    New-Item -ItemType Directory -Path $studioData -Force | Out-Null
    $studioLog = Join-Path $studioData ('server-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    $studioProcess = Start-Process -FilePath $studioPython -ArgumentList ('"' + $studioServer + '"') -WorkingDirectory $studioRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $studioLog -RedirectStandardError ($studioLog + '.error')
    [System.IO.File]::WriteAllText($studioPidFile, [string]$studioProcess.Id)
    for ($studioAttempt = 0; $studioAttempt -lt 20; $studioAttempt++) {
        try {
            $studioState = Invoke-RestMethod -Uri ($studioUrl + '/api/state') -TimeoutSec 1
            if ($studioState.token -and $null -ne $studioState.jobs) {
                if (-not $NoBrowser) { Start-Process $studioUrl }
                Write-Host 'Round Studio is running locally. Use Stop Round Studio.cmd when finished.'
                exit 0
            }
        } catch {}
        $studioProcess.Refresh()
        if ($studioProcess.HasExited) { throw ('Round Studio could not start. See ' + $studioLog + '.error') }
        Start-Sleep -Milliseconds 500
    }
    throw ('Round Studio did not respond. See ' + $studioLog + '.error')
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host $_.ScriptStackTrace
    exit 1
}
