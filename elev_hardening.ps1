# One-shot hardening 2026-09-15 (run as admin via UAC):
#   1. UtilityHub / UtilityHubWatchdog tasks: restart on failure (1 min interval, up to 10x)
#      - covers silent pythonw deaths (2026-09-15 ~06:15, no WER record, watchdog dead too)
#   2. Enable TaskScheduler operational event log (was disabled - no task history existed)
#   3. Restart UtilityHub to load the hotkeys.py F24 gating fix
$ErrorActionPreference = 'Continue'
Start-Transcript -Path "$PSScriptRoot\logs\elev_hardening.log" -Force

foreach ($name in 'UtilityHub', 'UtilityHubWatchdog') {
    try {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        $t.Settings.RestartInterval = 'PT1M'
        $t.Settings.RestartCount = 10
        Set-ScheduledTask -InputObject $t -ErrorAction Stop | Out-Null
        $t2 = Get-ScheduledTask -TaskName $name
        Write-Output "$name : RestartInterval=$($t2.Settings.RestartInterval) RestartCount=$($t2.Settings.RestartCount)"
    } catch {
        Write-Output "$name FAILED: $($_.Exception.Message)"
    }
}

wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
Write-Output "TaskScheduler operational log enabled: $LASTEXITCODE"

# --- restart hub (elevated process: taskkill from a normal shell would be denied) ---
$root = Split-Path -Parent $PSScriptRoot
$hubLog = Join-Path $root '5UtilityHub\logs'
foreach ($pf in 'watchdog.pid', 'hub.pid') {
    $p = Join-Path $hubLog $pf
    if (Test-Path $p) {
        $pidToKill = (Get-Content $p -ErrorAction SilentlyContinue) -as [int]
        if ($pidToKill) { taskkill /f /pid $pidToKill 2>$null | Out-Null }
    }
}
Start-Sleep -Seconds 2
schtasks /run /tn UtilityHub
Start-Sleep -Seconds 10
Write-Output ('hub.pid now: ' + (Get-Content (Join-Path $hubLog 'hub.pid') -ErrorAction SilentlyContinue))
Get-Content (Join-Path $hubLog 'hub.log') -Tail 4
Stop-Transcript
