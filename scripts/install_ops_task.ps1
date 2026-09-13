param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$TaskRoot = Split-Path -Parent $PSScriptRoot
$TaskName = 'Othryss Local Services'
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing -and @($Existing.Actions | Where-Object { $_.Arguments -eq '-m othryss.ops run' -and $_.WorkingDirectory -eq $TaskRoot }).Count -ne 1) { throw 'Existing task identity differs; refusing to replace it' }
if ($Remove) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; return }
$Python = (Get-Command pythonw.exe -ErrorAction Stop).Source
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $Python -Argument '-m othryss.ops run' -WorkingDirectory $TaskRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$RecoveryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([timespan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($Trigger,$RecoveryTrigger) -Principal $Principal -Settings $Settings -Description 'Read-only Othryss explorer, collectors and scheduled backups. Does not launch trading bots.' -Force | Select-Object TaskName,State
