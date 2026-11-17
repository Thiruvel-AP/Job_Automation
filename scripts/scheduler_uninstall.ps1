# Removes the Windows scheduled task "career-ops discover" created by scheduler_install.ps1. YOU run this from PowerShell; the repo never does.
$ErrorActionPreference = "Stop"
$TaskName = "career-ops discover"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
} else {
    Write-Host "No task named '$TaskName'; nothing to remove."
}
