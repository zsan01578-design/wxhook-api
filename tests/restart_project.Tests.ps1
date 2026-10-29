$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\restart-project.ps1')

function Assert-True($Condition, $Message) { if (-not $Condition) { throw $Message } }
$root = 'D:\Project With Spaces\wxhook'
$venv = Join-Path $root '.venv\Scripts\python.exe'
$main = Join-Path $root 'main.py'
function Process-Record($Id, $Executable, $Command, $Parent = 0) {
    [pscustomobject]@{ProcessId=$Id; ParentProcessId=$Parent; ExecutablePath=$Executable; CommandLine=$Command; Name='python.exe'; CreationDate='original'}
}
$snapshot = @(
    (Process-Record 101 $venv ('"' + $venv + '" main.py')),
    (Process-Record 102 'C:\Python312\python.exe' ('python.exe "' + $main + '"')),
    (Process-Record 103 'C:\Python312\python.exe' 'python.exe main.py' 101),
    (Process-Record 104 'C:\Python312\python.exe' 'python.exe main.py'),
    (Process-Record 105 'D:\Other\.venv\Scripts\python.exe' 'python.exe main.py'),
    (Process-Record 106 $venv 'python.exe unrelated.py'),
    (Process-Record 107 $venv ('python.exe -c "' + $main + '"')),
    (Process-Record 108 $venv 'python.exe -u -m wxhook_api')
)
$ids = @(Get-ProjectProcesses $root $snapshot | ForEach-Object { $_.ProcessId })
Assert-True (($ids -join ',') -eq '101,102,103,108') 'Project identification included unrelated Python or missed a venv child.'
Write-Output 'process ownership: passed'

$script:current = $snapshot[0]
function Get-CimInstance { param($Filter, $ErrorAction) $script:current }
Assert-True (Test-SameProcess $snapshot[0]) 'Original PID was not recognized.'
$script:current = Process-Record 101 $venv ('"' + $venv + '" main.py')
$script:current.CreationDate = 'reused-pid'
Assert-True (-not (Test-SameProcess $snapshot[0])) 'PID reuse was accepted.'
Write-Output 'PID reuse protection: passed'

$previousMediaRoot = $env:WXHOOK_MEDIA_PARSER_ROOT
try {
    $env:WXHOOK_MEDIA_PARSER_ROOT = 'E:\Media Parser'
    Assert-True ((Get-MediaParserRoot $root) -eq 'E:\Media Parser') 'Configured media-parser root was ignored.'
    Remove-Item Env:WXHOOK_MEDIA_PARSER_ROOT
    Assert-True ((Get-MediaParserRoot $root) -eq 'D:\Project With Spaces\media-parser') 'Default sibling media-parser root is incorrect.'
} finally {
    if ($null -ne $previousMediaRoot) { $env:WXHOOK_MEDIA_PARSER_ROOT = $previousMediaRoot }
    else { Remove-Item Env:WXHOOK_MEDIA_PARSER_ROOT -ErrorAction SilentlyContinue }
}
$mediaLauncher = Join-Path $root 'scripts\media-parser-server.py'
$mediaSnapshot = @(
    (Process-Record 201 $venv ('"' + $venv + '" "' + $mediaLauncher + '"')),
    (Process-Record 202 'C:\Python312\python.exe' 'python.exe scripts\media-parser-server.py'),
    (Process-Record 203 $venv 'python.exe unrelated.py')
)
$mediaIds = @(Get-MediaParserProcesses $root $mediaSnapshot | ForEach-Object { $_.ProcessId })
Assert-True (($mediaIds -join ',') -eq '201') 'media-parser ownership included unrelated Python.'
Write-Output 'media-parser root and ownership: passed'

function Update-ProjectCode { param($Root) throw 'simulated failed pull' }
function Stop-ProjectService { param($Root) throw 'BUG: stopped service after failed pull' }
function Start-MediaParserService { param($Root, $Python) throw 'BUG: started media-parser after failed pull' }
function Start-ProjectService { param($Root, $Python, $Configuration) throw 'BUG: started service after failed pull' }
try { Restart-Project $root; throw 'Expected failed pull' } catch {
    Assert-True ($_.Exception.Message -eq 'simulated failed pull') 'Update failure did not preserve the old service.'
}
Write-Output 'update failure preserves running service: passed'

# Run the complete preparation/restart order with an executable stub. It must
# not install dependencies, update a real checkout or stop any real process.
$scratch = Join-Path $env:TEMP ('wxhook-restart-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $scratch '.venv\Scripts') -Force | Out-Null
$source = @'
using System;
using System.IO;
class Stub {
    static int Main(string[] args) {
        if (args.Length > 1 && args[0] == "-c" && args[1].Contains("service_url"))
            Console.WriteLine("{\"url\":\"http://127.0.0.1:8000\",\"revision\":\"test\",\"hook\":\"test\"}");
        return 0;
    }
}
'@
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$code = Join-Path $scratch 'Stub.cs'
[IO.File]::WriteAllText($code, $source)
& $compiler /nologo ('/out:' + (Join-Path $scratch '.venv\Scripts\python.exe')) $code
if ($LASTEXITCODE -ne 0) { throw 'Stub compilation failed.' }
$script:events = @()
$script:failDependencies = $false
function Invoke-Checked {
    param($File, $Arguments)
    if (($Arguments -join ' ') -eq '-m pip check') {
        $script:events += 'dependencies'
        if ($script:failDependencies) { throw 'simulated dependency failure' }
    }
}
function Update-ProjectCode { param($Root) $script:events += 'pull' }
function Stop-ProjectService { param($Root) $script:events += 'stop' }
function Start-MediaParserService { param($Root, $Python) $script:events += 'start-media' }
function Start-ProjectService { param($Root, $Python, $Configuration) $script:events += 'start' }
Restart-Project $scratch
Assert-True (($script:events -join ',') -eq 'pull,dependencies,stop,start-media,start') 'Restart order is incorrect.'
$script:events = @()
Restart-Project $scratch -SkipUpdate
Assert-True (($script:events -join ',') -eq 'dependencies,stop,start-media,start') 'Local mode unexpectedly pulled code.'
$script:events = @()
$script:failDependencies = $true
try { Restart-Project $scratch; throw 'Expected dependency failure' } catch {
    Assert-True ($_.Exception.Message -eq 'simulated dependency failure') 'Dependency failure handling is incorrect.'
}
Assert-True (($script:events -join ',') -eq 'pull,dependencies') 'Dependency failure stopped the old service.'
Write-Output 'restart ordering, local mode, and dependency failure: passed'
