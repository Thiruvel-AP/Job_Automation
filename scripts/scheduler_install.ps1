# Creates the Windows scheduled task "career-ops discover": twice a day, 07:45 and 17:45, it runs scripts/discover.sh inside WSL.
# YOU run this, once, from a PowerShell prompt:   powershell -ExecutionPolicy Bypass -File scripts\scheduler_install.ps1
# Nothing in the repo runs it. Undo with scripts\scheduler_uninstall.ps1.
$ErrorActionPreference = "Stop"
$TaskName = "career-ops discover"
$Script   = "/mnt/d/VS_Code/DataScience/Projects/career-ops/scripts/discover.sh"

$action   = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-e bash -lc `"$Script`""
$triggers = @("07:45", "17:45") | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "career-ops: find jobs (discover.sh). It never tailors or applies." -Force | Out-Null
Write-Host "Registered '$TaskName' at 07:45 and 17:45. Test it: Start-ScheduledTask -TaskName '$TaskName', then read data/logs/discover_<today>.log"
