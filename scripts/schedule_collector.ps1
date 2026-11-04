# Registers the HX2605 collector as Windows scheduled tasks (run from the repo root).
#
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_collector.ps1                          # install
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_collector.ps1 -BackupDir E:\hx2605     # backups to another drive
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_collector.ps1 -Remove                  # uninstall
#
# Tasks run as YOU, only while you are logged in, hidden (pythonw), and catch up after sleep/wake.
# This script is never run automatically. Check status:  python -m scripts.collector_status
# Intervals must match INTERVAL_MINUTES in scripts/run_collect.py (tests/test_status.py checks this).
param(
    [string]$Python = (Join-Path $PSScriptRoot '..\.venv\Scripts\pythonw.exe'),
    [string]$BackupDir = '',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backupArgs = '-m scripts.backup_db'
if ($BackupDir) { $backupArgs += " --dest `"$BackupDir`"" }
$jobs = @(
    @{ Name = 'quotes';      Args = '-m scripts.run_collect --profile quotes';      Minutes = 10 },
    @{ Name = 'rss';         Args = '-m scripts.run_collect --profile rss';         Minutes = 15 },
    @{ Name = 'discovery';   Args = '-m scripts.run_collect --profile discovery';   Minutes = 180 },
    @{ Name = 'gdelt';       Args = '-m scripts.run_collect --profile gdelt';       Minutes = 60 },
    @{ Name = 'resolutions'; Args = '-m scripts.run_collect --profile resolutions'; Minutes = 60 },
    @{ Name = 'backup';      Args = $backupArgs;                                    DailyAt = '03:00' }
)

foreach ($job in $jobs) {
    $taskName = "HX2605-$($job.Name)"
    if ($Remove) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "removed $taskName"
        continue
    }
    if (-not (Test-Path $Python)) { throw "Python not found: $Python (pass -Python <path to pythonw.exe>)" }
    $action = New-ScheduledTaskAction -Execute (Resolve-Path $Python).Path -Argument $job.Args -WorkingDirectory $repo
    if ($job.DailyAt) {
        $trigger = New-ScheduledTaskTrigger -Daily -At $job.DailyAt
    } else {
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $job.Minutes) -RepetitionDuration (New-TimeSpan -Days 3650)
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "HX2605 data collection ($($job.Name))" -Force | Out-Null
    Write-Host "registered $taskName"
}
if (-not $Remove) {
    Write-Host "`nKeep the laptop plugged in with sleep set to 'Never'."
    Write-Host "Health: python -m scripts.collector_status   |   log: artifacts\research\collector_history.jsonl"
}
