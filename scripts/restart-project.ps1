[CmdletBinding()]
param([switch]$Local)

$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $File" }
}

function Get-ProjectProcesses {
    param([string]$Root, [object[]]$Snapshot)
    $main = Join-Path $Root 'main.py'
    $interpreters = @((Join-Path $Root '.venv\Scripts\python.exe'), (Join-Path $Root '.venv\Scripts\pythonw.exe'))
    foreach ($item in $Snapshot) {
        if ($item.Name -notin @('python.exe', 'pythonw.exe')) { continue }
        $parts = @([regex]::Matches([string]$item.CommandLine, '"([^"]*)"|(\S+)') | ForEach-Object { $_.Value.Trim('"') })
        $index = 1
        while ($index -lt $parts.Count -and $parts[$index] -in @('-u', '-B', '-E', '-I', '-s', '-S', '-O', '-OO')) { $index++ }
        if ($index -ge $parts.Count) { continue }
        $absoluteMain = $parts[$index] -eq $main
        $relativeMain = $parts[$index] -in @('main.py', '.\main.py', './main.py')
        $module = $parts[$index] -eq '-m' -and $index + 1 -lt $parts.Count -and $parts[$index + 1] -in @('wxhook_api', 'wxhook_api.launcher')
        $parent = $Snapshot | Where-Object { $_.ProcessId -eq $item.ParentProcessId } | Select-Object -First 1
        $ownInterpreter = $item.ExecutablePath -in $interpreters -or ($parent -and $parent.ExecutablePath -in $interpreters)
        if ($absoluteMain -or ($ownInterpreter -and ($relativeMain -or $module))) { $item }
    }
}

function Get-ProcessSnapshot {
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'")
}

function Test-SameProcess {
    param($Expected)
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($Expected.ProcessId)" -ErrorAction Stop
    return ($null -ne $current -and $current.CreationDate -eq $Expected.CreationDate -and
        $current.ExecutablePath -eq $Expected.ExecutablePath -and $current.CommandLine -eq $Expected.CommandLine)
}

function Stop-ProjectService {
    param([string]$Root)
    $targets = @(Get-ProjectProcesses $Root (Get-ProcessSnapshot))
    if (-not $targets.Count) { Write-Host 'No existing project service found.'; return }
    $statePath = Join-Path $Root 'data\runtime\service.json'
    if (Test-Path -LiteralPath $statePath) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            $registered = $targets | Where-Object { $_.ProcessId -eq $state.pid } | Select-Object -First 1
            if ($registered -and $state.project -eq $Root -and $state.token -match '^[a-f0-9]{32}$' -and (Test-SameProcess $registered)) {
                $stopPath = Join-Path $Root "data\runtime\stop-$($state.token).request"
                [System.IO.File]::WriteAllText($stopPath, $state.token)
                Write-Host "Waiting for service PID $($state.pid) to shut down cleanly..."
                $deadline = [DateTime]::UtcNow.AddSeconds(45)
                while ((Test-SameProcess $registered) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
            }
        } catch { Write-Warning "Graceful shutdown unavailable: $($_.Exception.Message)" }
    }
    # Old versions lack the handshake. Never stop every Python process or the
    # entire process tree: WeChat must remain running.
    foreach ($target in $targets) {
        if (Test-SameProcess $target) {
            Write-Host "Stopping legacy or unresponsive project PID $($target.ProcessId)..."
            Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $remaining = @(Get-ProjectProcesses $Root (Get-ProcessSnapshot))
        if (-not $remaining.Count) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The old project process has not stopped. A second instance was not started.'
}

function Test-ServicePort {
    param([string]$Url)
    $uri = [Uri]$Url
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return @($listeners | Where-Object { $_.Port -eq $uri.Port }).Count -gt 0
}

function Start-ProjectService {
    param([string]$Root, [string]$Python, $Configuration)
    if (Test-ServicePort $Configuration.url) { throw "Port still occupied at $($Configuration.url). No unrelated process was stopped." }
    $main = Join-Path $Root 'main.py'
    $outLog = Join-Path $Root 'data\startup.stdout.log'
    $errLog = Join-Path $Root 'data\startup.stderr.log'
    $process = Start-Process -FilePath $Python -ArgumentList @('"' + $main + '"') -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $headers = @{}
    if ($env:WXHOOK_ADMIN_TOKEN) { $headers['Authorization'] = 'Bearer ' + $env:WXHOOK_ADMIN_TOKEN }
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        try {
            $health = Invoke-RestMethod -Uri ($Configuration.url + '/health') -Headers $headers -TimeoutSec 2 -UseBasicParsing
            if ($health.status -eq 'ok' -and $health.runtime_revision -eq $Configuration.revision -and $health.hook_base_url -eq $Configuration.hook) {
                Write-Host "Restart complete: $($Configuration.url)/"
                Write-Host "Logs: $outLog and $errLog"
                return
            }
        } catch { }
        $process.Refresh()
        if ($process.HasExited) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Service startup was not confirmed. Check $errLog and data\wxhook.log."
}

function Restart-Project {
    param([string]$Root, [switch]$SkipUpdate)
    Write-Host '[1/4] Updating code...'
    if (-not $SkipUpdate) {
        Invoke-Checked 'git.exe' @('pull', '--ff-only')
    } else { Write-Host 'Local restart selected; Git update skipped.' }

    Write-Host '[2/4] Preparing dependencies; the old service is still running...'
    $python = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { Invoke-Checked 'py.exe' @('-3.12', '-m', 'venv', (Join-Path $Root '.venv')) }
    $digest = @'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256(b''.join((root / name).read_bytes() for name in ('requirements-runtime.lock', 'pyproject.toml'))).hexdigest()
stamp = root / '.venv' / '.wxhook-install-state'
sys.exit(0 if stamp.is_file() and stamp.read_text(encoding='ascii').strip() == digest else 1)
'@
    & $python -c $digest $Root
    if ($LASTEXITCODE -ne 0) {
        Invoke-Checked $python @('-m', 'pip', 'install', '-r', (Join-Path $Root 'requirements-runtime.lock'))
        Invoke-Checked $python @('-m', 'pip', 'install', '--no-deps', '-e', $Root)
    }
    Invoke-Checked $python @('-m', 'pip', 'check')
    Invoke-Checked $python @('-c', "import hashlib,pathlib,sys; r=pathlib.Path(sys.argv[1]); (r/'.venv'/'.wxhook-install-state').write_text(hashlib.sha256(b''.join((r/n).read_bytes() for n in ('requirements-runtime.lock','pyproject.toml'))).hexdigest(),encoding='ascii')", $Root)
    if (-not $env:WXHOOK_SYNC_INTERVAL) { $env:WXHOOK_SYNC_INTERVAL = '180' }
    $probe = "import json; from urllib.parse import urlsplit; from wxhook_api.core.config import load_settings; from wxhook_api.core.runtime import source_revision; from wxhook_api.launcher import service_url,load_launcher_settings; s=load_settings(); load_launcher_settings(); assert urlsplit(s.hook_base_url).hostname in {'localhost','127.0.0.1','::1'}, 'Hook must be local'; print(json.dumps(dict(url=service_url(s),hook=s.hook_base_url,revision=source_revision())))"
    $configuration = (& $python -c $probe) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $configuration.url) { throw 'Cannot load project configuration. The old service was not stopped.' }
    New-Item -ItemType Directory -Path (Join-Path $Root 'data') -Force | Out-Null

    Write-Host '[3/4] Stopping the old project service...'
    Stop-ProjectService $Root
    Write-Host '[4/4] Starting the updated service...'
    Start-ProjectService $Root $python $configuration
}

if ($MyInvocation.InvocationName -ne '.') {
    $root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $hash = [System.Security.Cryptography.SHA256]::Create()
    $key = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant())))).Replace('-', '')
    $hash.Dispose()
    $mutex = New-Object System.Threading.Mutex($false, "Local\WxHook-Restart-$key")
    $locked = $false
    try {
        try { $locked = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { throw 'An update/restart is already in progress. Wait for that window to finish.' }
        Push-Location -LiteralPath $root
        try { Restart-Project $root -SkipUpdate:$Local } finally { Pop-Location }
    } catch {
        Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
        Write-Host 'If Git update failed, the existing service was left running. Use start-local.bat to restart local code.'
        exit 1
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
