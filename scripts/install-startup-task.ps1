[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = 'WxHook API',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$projectPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
$mainPath = Join-Path $projectPath 'main.py'
$arguments = '"' + $mainPath + '"'
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Actions.Count -ne 1 -or
        $existing.Actions[0].Execute -ne $pythonPath -or
        $existing.Actions[0].Arguments -ne $arguments) {
        throw "A task named '$TaskName' already exists with a different command. Choose another -TaskName."
    }
}

if ($Remove) {
    if ($existing -and $PSCmdlet.ShouldProcess($TaskName, 'Remove this WxHook startup task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    return
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $mainPath -PathType Leaf)) {
    throw 'Create the project virtual environment and install dependencies first.'
}
$userName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userName
$principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if ($PSCmdlet.ShouldProcess($TaskName, "Register login startup and failure recovery for $mainPath")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-Output "Registered '$TaskName'. It starts at the next login; failure restarts wait one minute (at most three)."
}
