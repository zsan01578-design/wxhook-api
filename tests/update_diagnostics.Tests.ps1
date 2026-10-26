$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\restart-project.ps1')

function Assert-True($Condition, $Message) { if (-not $Condition) { throw $Message } }
$sample = @'
error: Your local changes to the following files would be overwritten by merge:
    docs/GUIDE.md
    src/中文 文件.py
Please commit your changes or stash them before you merge.
error: The following untracked working tree files would be overwritten by merge:
    src/favorites.py
Please move or remove them before you merge.
Aborting
'@
$diagnostic = Get-GitUpdateDiagnostic $sample
Assert-True ($diagnostic.Reason -eq 'files' -and $diagnostic.Files.Count -eq 3) 'Mixed file conflict list was not recognized.'
Assert-True ($diagnostic.Files[1].Path -eq 'src/中文 文件.py') 'Chinese or spaced filename was damaged.'
Assert-True ($diagnostic.Files[0].Kind -eq 'modified' -and $diagnostic.Files[2].Kind -eq 'untracked') 'Conflict categories were mixed up.'
Assert-True ((Get-GitUpdateDiagnostic "fatal: unable to access 'https://example.invalid/repo': Recv failure: Connection was reset").Reason -eq 'network') 'Network failure was misclassified.'
Assert-True ((Get-GitUpdateDiagnostic 'fatal: Authentication failed').Reason -eq 'authentication') 'Authentication failure was misclassified.'
Assert-True ((Get-GitUpdateDiagnostic "fatal: repository 'https://example.invalid/private' not found").Reason -eq 'authentication') 'Inaccessible repository was misclassified.'
Assert-True ((Get-GitUpdateDiagnostic 'fatal: Not possible to fast-forward, aborting.').Reason -eq 'diverged') 'Diverged branch was misclassified.'
Assert-True ((Get-GitUpdateDiagnostic 'fatal: Unable to create .git/index.lock: File exists').Reason -eq 'locked') 'Git lock was misclassified.'
Assert-True ((Get-GitUpdateDiagnostic 'error: Pulling is not possible because you have unmerged files.').Reason -eq 'unmerged') 'Unresolved merge was misclassified.'
$access = Get-GitUpdateDiagnostic "error: unable to unlink old 'src/locked.py': Permission denied"
Assert-True ($access.Reason -eq 'files' -and $access.Files[0].Path -eq 'src/locked.py' -and $access.Files[0].Kind -eq 'access') 'Locked file path was lost.'
Assert-True ((Get-GitUpdateDiagnostic 'unexpected failure').Reason -eq 'unknown') 'Unknown error guessed a cause.'
Write-Output 'diagnostic categories and filenames: passed'

# Local repositories only. No fetch from GitHub, user configuration, or service
# process is involved; all mutations stay in this unique scratch directory.
$scratch = Join-Path $PSScriptRoot ('..\.tools\update-diagnostics-' + [Guid]::NewGuid().ToString('N'))
$scratch = [IO.Path]::GetFullPath($scratch)
$remote = Join-Path $scratch 'remote.git'
$author = Join-Path $scratch 'author'
$checkout = Join-Path $scratch 'Checkout With Spaces'
New-Item -ItemType Directory -Path $scratch -Force | Out-Null
function Test-Git {
    param([string[]]$Arguments)
    & git.exe @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Test fixture Git command failed ($LASTEXITCODE)." }
}
Test-Git @('init', '--bare', $remote)
Test-Git @('init', '-b', 'main', $author)
$unicodeName = '中文 文件.txt'
[IO.File]::WriteAllText((Join-Path $author 'tracked.txt'), "base`n")
[IO.File]::WriteAllText((Join-Path $author $unicodeName), "base`n")
Test-Git @('-C', $author, 'add', 'tracked.txt', $unicodeName)
Test-Git @('-C', $author, '-c', 'user.name=Update Tests', '-c', 'user.email=update-tests@example.invalid', 'commit', '-m', 'base')
Test-Git @('-C', $author, 'remote', 'add', 'origin', $remote)
Test-Git @('-C', $author, 'push', '-u', 'origin', 'main')
Test-Git @('clone', '--branch', 'main', $remote, $checkout)

# Introduce both types of collision seen in the user's screenshot.
[IO.File]::WriteAllText((Join-Path $checkout 'tracked.txt'), "keep local edits`n")
[IO.File]::WriteAllText((Join-Path $checkout $unicodeName), "keep local Chinese file`n")
[IO.File]::WriteAllText((Join-Path $checkout 'new.txt'), "keep local untracked file`n")
[IO.File]::WriteAllText((Join-Path $author 'tracked.txt'), "upstream edit`n")
[IO.File]::WriteAllText((Join-Path $author $unicodeName), "upstream Chinese file`n")
[IO.File]::WriteAllText((Join-Path $author 'new.txt'), "upstream new file`n")
Test-Git @('-C', $author, 'add', 'tracked.txt', 'new.txt', $unicodeName)
Test-Git @('-C', $author, '-c', 'user.name=Update Tests', '-c', 'user.email=update-tests@example.invalid', 'commit', '-m', 'upstream')
Test-Git @('-C', $author, 'push', 'origin', 'main')
try { Update-ProjectCode $checkout; throw 'Expected update failure.' } catch {
    Assert-True ($_.Exception.Message -match '^Git update failed') 'Git failure was not reported.'
}
$report = Get-Content -LiteralPath (Join-Path $checkout 'data\update-last-error.log') -Raw -Encoding UTF8
Assert-True ($report.Contains('[本地已修改] tracked.txt')) 'Tracked filename missing from report.'
Assert-True ($report.Contains("[本地已修改] $unicodeName")) 'Actual Git output lost the Chinese filename.'
Assert-True ($report.Contains('[本地未跟踪，同名文件阻止更新] new.txt')) 'Untracked filename missing from report.'
Assert-True ([IO.File]::ReadAllText((Join-Path $checkout 'tracked.txt')) -eq "keep local edits`n") 'Tracked file was overwritten.'
Assert-True ([IO.File]::ReadAllText((Join-Path $checkout 'new.txt')) -eq "keep local untracked file`n") 'Untracked file was overwritten.'
Assert-True (-not $report.Contains('keep local')) 'File contents leaked into report.'
Write-Output 'real local pull conflicts, report, and preserved files: passed'

# A clean checkout must still update successfully.
$clean = Join-Path $scratch 'clean'
Test-Git @('clone', '--branch', 'main', $remote, $clean)
Update-ProjectCode $clean
Assert-True (-not (Test-Path -LiteralPath (Join-Path $clean 'data\update-last-error.log'))) 'Successful update produced an error report.'
Write-Output 'successful Git update: passed'

function Invoke-UpdateGit {
    param($Root, $Arguments)
    return [pscustomobject]@{ExitCode=128; Stdout=''; Stderr="fatal: unable to access 'https://secret-token@example.invalid/repo': Connection was reset"}
}
try { Update-ProjectCode $clean; throw 'Expected network failure.' } catch {
    Assert-True ($_.Exception.Message -match '^Git update failed') 'Network failure was swallowed.'
}
$report = Get-Content -LiteralPath (Join-Path $clean 'data\update-last-error.log') -Raw -Encoding UTF8
Assert-True ($report.Contains('连接远程仓库失败')) 'Network diagnosis missing.'
Assert-True (-not $report.Contains('secret-token') -and -not $report.Contains('example.invalid')) 'Raw credential-bearing URL leaked into saved report.'
Write-Output 'network report excludes raw credentials: passed'
