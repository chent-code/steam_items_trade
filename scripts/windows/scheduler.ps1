<#
UUYP scheduler control (ASCII only; safe for Windows PowerShell 5.1).
Usage:
  powershell -NoProfile -ExecutionPolicy Bypass -File scheduler.ps1 -Action status
  powershell -NoProfile -ExecutionPolicy Bypass -File scheduler.ps1 -Action stop
  powershell -NoProfile -ExecutionPolicy Bypass -File scheduler.ps1 -Action start
#>
param(
    [ValidateSet('status','stop','start','restart')]
    [string]$Action = 'status',
    [string]$TaskName = 'UUYP_auto'
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$logDir = Join-Path (Join-Path $root 'data') 'logs'
$consoleLog = Join-Path $logDir 'auto_console.log'

function Get-Task {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
function Get-RunningProcs {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -like 'python*' -and $_.CommandLine -like '*watcher_inv.py auto*'
    }
}
function Show-Status {
    $t = Get-Task
    if (-not $t) {
        Write-Output ('TASK: ' + $TaskName + ' NOT FOUND (nothing scheduled)')
    } else {
        $i = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Output ('TASK: ' + $t.TaskName + '  STATE=' + $t.State)
        if ($i) {
            Write-Output ('  LastRun=' + $i.LastRunTime + '  LastResult=' + $i.LastTaskResult + '  NextRun=' + $i.NextRunTime)
        }
        Write-Output ('  Enabled=' + (-not ($t.State -eq 'Disabled')))
    }
    $p = @(Get-RunningProcs)
    Write-Output ('RUNNING ROUNDS: ' + $p.Count)
    foreach ($x in $p) { Write-Output ('  PID=' + $x.ProcessId) }
    if (Test-Path $consoleLog) {
        Write-Output '--- last 6 lines of auto_console.log ---'
        Get-Content $consoleLog -Tail 6 -Encoding UTF8
    }
}

switch ($Action) {
    'status' { Show-Status }
    'stop' {
        $t = Get-Task
        if ($t) {
            # Stop running instance cleanly, then Disable blocks all future triggers.
            try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
            try {
                Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
                Write-Output ('[OK] Scheduled task DISABLED: ' + $TaskName)
            } catch {
                Write-Output ('[WARN] Disable failed (try Run as Administrator): ' + $_.Exception.Message)
            }
        } else {
            Write-Output ('[i] Task not found: ' + $TaskName)
        }
        # Fallback: kill any leftover python running our loop (exact commandline match only).
        $p = @(Get-RunningProcs)
        foreach ($x in $p) {
            try { Stop-Process -Id $x.ProcessId -Force -ErrorAction Stop; Write-Output ('[OK] Killed running round PID=' + $x.ProcessId) } catch {}
        }
        Write-Output '--- status after stop ---'
        Show-Status
    }
    'start' {
        $t = Get-Task
        if ($t) {
            try {
                Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
                Write-Output ('[OK] Scheduled task ENABLED: ' + $TaskName)
            } catch {
                Write-Output ('[WARN] Enable failed (try Run as Administrator): ' + $_.Exception.Message)
            }
        } else {
            Write-Output ('[WARN] Task ' + $TaskName + ' not found. Create it first (see README).')
        }
        Write-Output '--- status after start ---'
        Show-Status
    }
    'restart' {
        & $PSCommandPath -Action stop -TaskName $TaskName
        & $PSCommandPath -Action start -TaskName $TaskName
    }
}
