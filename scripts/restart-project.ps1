[CmdletBinding()]
param([switch]$Local)

$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $File" }
}

function Invoke-UpdateGit {
    param([string]$Root, [string[]]$Arguments)
    # Drain both streams concurrently: redirected Git output can fill either
    # pipe. A child-only locale keeps diagnostics stable without changing Git
    # configuration, credentials, or the calling console's encoding.
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = 'git.exe'
    $start.WorkingDirectory = $Root
    $start.Arguments = ($Arguments -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $start.EnvironmentVariables['LC_ALL'] = 'C'
    $start.EnvironmentVariables['LANG'] = 'C'
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Cannot start git.exe.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        return [pscustomobject]@{ExitCode=$process.ExitCode; Stdout=$stdout.Result; Stderr=$stderr.Result}
    } finally { $process.Dispose() }
}

function Get-GitUpdateDiagnostic {
    param([string]$Text)
    $files = New-Object 'System.Collections.Generic.List[object]'
    $kind = ''
    foreach ($line in ($Text -split '\r?\n')) {
        if ($line -match 'local changes to the following files would be overwritten') { $kind = 'modified'; continue }
        if ($line -match 'untracked working tree files would be (overwritten|removed)') { $kind = 'untracked'; continue }
        if ($line -match '(?:unable to (?:create file|unlink old)|cannot (?:stat|unlink)|unable to read) [''"](?<path>.+?)[''"]') {
            $files.Add([pscustomobject]@{Kind='access'; Path=$Matches['path']})
            $kind = ''; continue
        }
        if ($line -match 'unable to create file (?<path>.+): (?:Permission denied|Invalid argument)') {
            $files.Add([pscustomobject]@{Kind='access'; Path=$Matches['path']})
            $kind = ''; continue
        }
        if ($kind -and $line -match '^\s+\S') {
            $path = $line.Trim()
            if ($path.StartsWith('"')) {
                try { $path = ConvertFrom-Json $path -ErrorAction Stop } catch { }
            }
            $files.Add([pscustomobject]@{Kind=$kind; Path=$path})
        } else { $kind = '' }
    }
    $reason = 'unknown'
    if ($files.Count) { $reason = 'files' }
    elseif ($Text -match 'unmerged files|unresolved conflict|needs merge') { $reason = 'unmerged' }
    elseif ($Text -match 'not possible to fast-forward|divergent branches|refusing to merge unrelated histories') { $reason = 'diverged' }
    elseif ($Text -match 'Authentication failed|Permission denied \(publickey\)|could not read Username|terminal prompts disabled|repository .*not found|repository not found|returned error: (401|403)') { $reason = 'authentication' }
    elseif ($Text -match 'Could not resolve host|Failed to connect|Connection (was )?(reset|timed out|refused)|Recv failure|SSL (certificate|connect)|unable to access|Operation timed out') { $reason = 'network' }
    elseif ($Text -match 'index\.lock|another git process') { $reason = 'locked' }
    return [pscustomobject]@{Reason=$reason; Files=@($files.ToArray())}
}

function Update-ProjectCode {
    param([string]$Root)
    $result = Invoke-UpdateGit $Root @('-c', 'core.quotepath=false', '-c', 'color.ui=false', 'pull', '--ff-only')
    # Preserve the original Git output as well as the readable summary.
    if ($result.Stdout) { Write-Host $result.Stdout.TrimEnd() }
    if ($result.Stderr) { Write-Host $result.Stderr.TrimEnd() }
    if ($result.ExitCode -eq 0) { return }

    $detail = Get-GitUpdateDiagnostic ($result.Stdout + "`n" + $result.Stderr)
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $lines.Add('[更新失败] 拉取代码未完成，旧服务保持运行。')
    switch ($detail.Reason) {
        'files' {
            $lines.Add('原因：本地文件冲突或读写失败。出问题的文件（相对于项目目录）：')
            foreach ($file in $detail.Files) {
                $label = switch ($file.Kind) {
                    'modified' { '本地已修改' }
                    'untracked' { '本地未跟踪，同名文件阻止更新' }
                    default { '文件读写失败' }
                }
                $lines.Add("  [$label] $($file.Path)")
            }
            $lines.Add('处理：先备份并核对以上文件的本地改动，再更新。脚本不会自动删除或覆盖这些文件。')
        }
        'network' { $lines.Add('原因：连接远程仓库失败，请检查网络、代理或稍后重试。本次没有确认到文件冲突。') }
        'authentication' { $lines.Add('原因：仓库认证失败或没有访问权限，请检查 Git 登录和仓库地址。') }
        'diverged' { $lines.Add('原因：本地与远程提交无法直接快进，请先核对提交历史并处理分叉。') }
        'locked' { $lines.Add('原因：Git 工作区被锁定，请确认是否有其他 Git 更新任务正在运行。') }
        'unmerged' {
            $lines.Add('原因：工作区存在尚未解决的合并冲突，请先处理以下文件：')
            try {
                $unmerged = Invoke-UpdateGit $Root @('-c', 'core.quotepath=false', 'diff', '--name-only', '--diff-filter=U')
                if ($unmerged.ExitCode -eq 0 -and $unmerged.Stdout.Trim()) {
                    foreach ($path in ($unmerged.Stdout.TrimEnd() -split '\r?\n')) { $lines.Add("  [未解决合并冲突] $path") }
                } else { $lines.Add('未能列出文件，请运行 git status 查看。') }
            } catch { $lines.Add('未能列出文件，请运行 git status 查看。') }
        }
        default { $lines.Add('原因：Git 返回失败，未能识别具体文件，请查看上方 Git 原始错误。') }
    }
    $lines.Add('只想重启现有版本，可双击 start-local.bat。')
    foreach ($line in $lines) { Write-Host $line -ForegroundColor Yellow }
    # Store filenames and diagnosis only, never file contents or raw output
    # that might contain credential-bearing repository URLs.
    try {
        $report = Join-Path $Root 'data\update-last-error.log'
        New-Item -ItemType Directory -Path (Split-Path -Parent $report) -Force | Out-Null
        $stamp = [DateTimeOffset]::Now.ToString('yyyy-MM-dd HH:mm:ss zzz')
        [IO.File]::WriteAllLines($report, @("时间：$stamp", "项目目录：$Root") + $lines.ToArray(), (New-Object System.Text.UTF8Encoding($true)))
        Write-Host "错误摘要已保存：$report"
    } catch { Write-Host '无法写入错误摘要文件，请复制上面的提示信息。' -ForegroundColor Yellow }
    throw "Git update failed (exit $($result.ExitCode)). See the file list and diagnosis above."
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

function Get-MediaParserRoot {
    param([string]$Root)
    $configured = $env:WXHOOK_MEDIA_PARSER_ROOT
    if ($configured) { return [System.IO.Path]::GetFullPath($configured) }
    return [System.IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $Root) 'media-parser'))
}

function Get-MediaParserProcesses {
    param([string]$Root, [object[]]$Snapshot)
    $launcher = Join-Path $Root 'scripts\media-parser-server.py'
    $interpreters = @((Join-Path $Root '.venv\Scripts\python.exe'), (Join-Path $Root '.venv\Scripts\pythonw.exe'))
    foreach ($item in $Snapshot) {
        if ($item.Name -notin @('python.exe', 'pythonw.exe')) { continue }
        if ($item.ExecutablePath -notin $interpreters) { continue }
        if (-not ([string]$item.CommandLine).Contains($launcher)) { continue }
        $item
    }
}

function Stop-MediaParserService {
    param([string]$Root)
    $targets = @(Get-MediaParserProcesses $Root (Get-ProcessSnapshot))
    foreach ($target in $targets) {
        if (Test-SameProcess $target) {
            Write-Host "Stopping media-parser PID $($target.ProcessId)..."
            Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        if (-not @(Get-MediaParserProcesses $Root (Get-ProcessSnapshot)).Count) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The old media-parser process has not stopped.'
}

function Start-MediaParserService {
    param([string]$Root, [string]$Python)
    $mediaRoot = Get-MediaParserRoot $Root
    Stop-MediaParserService $Root
    if (-not (Test-Path -LiteralPath (Join-Path $mediaRoot 'app.py'))) {
        Write-Host "media-parser not installed at $mediaRoot; parsing fallback stays disabled."
        return
    }
    $launcher = Join-Path $Root 'scripts\media-parser-server.py'
    if (-not (Test-Path -LiteralPath $launcher)) {
        Write-Warning "Missing $launcher; parsing fallback stays disabled."
        return
    }
    $port = 8051
    if ($env:WXHOOK_MEDIA_PARSER_PORT) {
        try { $port = [int]$env:WXHOOK_MEDIA_PARSER_PORT } catch { throw 'WXHOOK_MEDIA_PARSER_PORT must be an integer.' }
    }
    if ($port -lt 1 -or $port -gt 65535) { throw 'WXHOOK_MEDIA_PARSER_PORT is out of range.' }
    $health = "http://127.0.0.1:$port/api/health"
    if (Test-ServicePort $health) {
        Write-Warning "Port $port is already in use; media-parser was not started."
        return
    }
    $outLog = Join-Path $Root 'data\media-parser.stdout.log'
    $errLog = Join-Path $Root 'data\media-parser.stderr.log'
    $process = Start-Process -FilePath $Python -ArgumentList @('"' + $launcher + '"', '--root', ('"' + $mediaRoot + '"'), '--port', "$port") -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        try {
            $status = Invoke-RestMethod -Uri $health -TimeoutSec 2 -UseBasicParsing
            if ($status.status -eq 'ok') {
                $statePath = Join-Path $Root 'data\runtime\media-parser.json'
                New-Item -ItemType Directory -Path (Split-Path -Parent $statePath) -Force | Out-Null
                [IO.File]::WriteAllText($statePath, (@{pid=$process.Id; root=$mediaRoot; port=$port; started_at=(Get-Date).ToString('o')} | ConvertTo-Json))
                Write-Host "media-parser ready: $health (PID $($process.Id))"
                return
            }
        } catch { }
        $process.Refresh()
        if ($process.HasExited) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    Write-Warning "media-parser did not become ready. Check $outLog and $errLog; wxhook will still start."
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
    Write-Host '[1/5] Updating code...'
    if (-not $SkipUpdate) {
        Update-ProjectCode $Root
    } else { Write-Host 'Local restart selected; Git update skipped.' }

    Write-Host '[2/5] Preparing dependencies; the old service is still running...'
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

    Write-Host '[3/5] Stopping the old project service...'
    Stop-ProjectService $Root
    Write-Host '[4/5] Starting media-parser local service...'
    try {
        Start-MediaParserService $Root $python
    } catch {
        Write-Warning "media-parser lifecycle failed: $($_.Exception.Message); wxhook will still start."
    }
    Write-Host '[5/5] Starting the updated service...'
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
