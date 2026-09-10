# Register the admission shim as a boot task.  RUN ELEVATED.
#
#   Right-click PowerShell -> Run as administrator, then:
#     & <path-to>\register-boot-task.ps1
#
# BootTrigger, S4U, RunLevel Limited, restart 3x at 1-minute intervals, no
# execution time limit.
#
# S4U (not Interactive) is deliberate: the shim must come back headless after an
# unattended reboot, without waiting for a logon. An Interactive boot task that
# waits for a logon can quietly leave the service down for days after a forced
# reboot.
#
# RunLevel is Limited - the shim only makes HTTP calls and binds a high port, so
# it needs nothing elevated at runtime.
#
# No dependency on the planes being up first: the shim treats an unreachable
# plane as a valid state (logs it, refuses loads it cannot vouch for) and
# re-reads every plane on each admission, so it self-heals as they come up.

$ErrorActionPreference = "Stop"

$TaskName = "Admission Shim (boot)"
$TaskPath = "\"
$Root     = $PSScriptRoot
$VenvPy   = "$Root\.venv\Scripts\python.exe"
$Shim     = "$Root\admission_shim.py"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Not elevated. Re-run this script from an administrator PowerShell."
    exit 1
}
if (-not (Test-Path $VenvPy)) {
    Write-Error "venv missing at $VenvPy. Run: & $Root\start.ps1 -Setup"
    exit 1
}
if (-not (Test-Path $Shim)) { Write-Error "admission_shim.py missing at $Shim."; exit 1 }

$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Task already exists - unregistering so this re-registers cleanly."
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $VenvPy -Argument $Shim -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

Write-Host "Registered '$TaskName'."

# Stop any shim already running in a user session, so the task owns the port.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*admission_shim.py*' } |
    ForEach-Object {
        Write-Host "  stopping existing shim pid $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force
    }
Start-Sleep -Seconds 2

Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
Write-Host "Started. Waiting for it to bind :13304..."

$ok = $false
foreach ($i in 1..20) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:13304/_shim/status" -TimeoutSec 3
        $ok = $true
        break
    } catch { }
}

if ($ok) {
    Write-Host "OK - shim v$($r.shim_version) responding on :13304"
    foreach ($k in $r.planes.PSObject.Properties.Name) {
        $p = $r.planes.$k
        Write-Host ("  plane {0}: reachable={1} loaded={2}" -f $k, $p.reachable, ($p.loaded.name -join ', '))
    }
    if ($r.ctx_drift_warnings) { Write-Warning "ctx drift: $($r.ctx_drift_warnings -join '; ')" }
} else {
    Write-Warning "Task registered but :13304 did not answer. Check $Root\shim.log"
    Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath | Get-ScheduledTaskInfo |
        Select-Object LastRunTime, LastTaskResult, NumberOfMissedRuns | Format-List
}

Write-Host ""
Write-Host "To remove:  Unregister-ScheduledTask -TaskName '$TaskName' -TaskPath '$TaskPath' -Confirm:`$false"
