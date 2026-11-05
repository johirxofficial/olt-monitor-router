# ---------------------------------------------------------------------------
#  olt-monitor-router - start automatically when this Windows user logs in
#
#      powershell -ExecutionPolicy Bypass -File tools\autostart.ps1 install
#      powershell -ExecutionPolicy Bypass -File tools\autostart.ps1 remove
#      powershell -ExecutionPolicy Bypass -File tools\autostart.ps1 status
#
#  Two mechanisms, tried in order:
#
#   1. a per-user Scheduled Task.  This needs an *elevated* shell, and in
#      exchange Task Scheduler also restarts the monitor automatically if it
#      ever dies (once a minute) and starts it even before logon finishes;
#   2. a per-user Startup-folder shortcut (no administrator rights) that runs
#      start-bg.bat with a completely hidden window.
#
#  In both cases the monitor runs through pythonw.exe so no console window
#  appears, and run.py exits quietly when the port is already taken, so a
#  manual start.bat never fights with the auto-start copy.
#
#  Logs stay where they always are: logs\olt_monitor.log
# ---------------------------------------------------------------------------
param(
    [ValidateSet('install', 'remove', 'status')]
    [string]$Action = 'install',

    [int]$Port = 5002,

    [string]$TaskName = 'olt-monitor-router'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot   # the olt-monitor-router folder
Set-Location $Root

$StartupDir = [Environment]::GetFolderPath('Startup')
$StartupFile = Join-Path $StartupDir 'olt-monitor-router.vbs'

function Resolve-Interpreter {
    $venv = Join-Path $Root '.venv\Scripts\pythonw.exe'
    if (Test-Path $venv) {
        return @{ Exe = $venv; Args = ''; Label = $venv }
    }

    $pyw = (Get-Command pyw.exe -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($pyw) {
        return @{ Exe = $pyw.Source; Args = '-3.12 '; Label = "$($pyw.Source) -3.12" }
    }

    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($pythonw) {
        return @{ Exe = $pythonw.Source; Args = ''; Label = $pythonw.Source }
    }

    throw 'pythonw.exe was not found. Install Python 3.10+ and tick "Add python.exe to PATH".'
}

function Install-StartupEntry {
    if (-not (Test-Path $StartupDir)) {
        New-Item -ItemType Directory -Path $StartupDir -Force | Out-Null
    }
    $bat = Join-Path $Root 'start-bg.bat'
    $lines = @(
        "' olt-monitor-router - start the monitor hidden at logon",
        "' (installed by tools\autostart.ps1 - remove with autostart.bat remove)",
        'Dim shell, target',
        "target = ""$bat""",
        'Set shell = CreateObject("WScript.Shell")',
        'shell.CurrentDirectory = Left(target, InStrRev(target, "\"))',
        'shell.Run """" & target & """", 0, False'
    )
    Set-Content -Path $StartupFile -Value $lines -Encoding ASCII
    Write-Host "[*] startup entry installed: $StartupFile"
}

function Remove-StartupEntry {
    if (Test-Path $StartupFile) {
        Remove-Item $StartupFile -Force
        Write-Host "[*] startup entry removed: $StartupFile"
    }
}

switch ($Action) {

    'install' {
        $run = Resolve-Interpreter
        Write-Host "[*] interpreter: $($run.Label)"

        $installed = $false

        # ---- 1. scheduled task (needs administrator) ----------------------
        try {
            $arguments = "$($run.Args)run.py --port $Port"
            $taskAction = New-ScheduledTaskAction -Execute $run.Exe -Argument $arguments -WorkingDirectory $Root
            $trigger = New-ScheduledTaskTrigger -AtLogOn
            $settings = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable `
                -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount 999 `
                -RestartInterval (New-TimeSpan -Minutes 1)
            $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

            Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger `
                -Settings $settings -Principal $principal -Force | Out-Null
            Write-Host "[*] scheduled task '$TaskName' installed (auto-restart on crash is on)"
            Remove-StartupEntry
            Start-ScheduledTask -TaskName $TaskName
            $installed = $true
        } catch {
            Write-Host "[i] could not register a scheduled task ($($_.Exception.Message.Trim()))"
            Write-Host "[i] falling back to the Startup folder - no administrator rights needed"
        }

        # ---- 2. Startup folder (always works) ----------------------------
        if (-not $installed) {
            Install-StartupEntry
            Write-Host "[*] the monitor will start hidden at every logon"
            Write-Host "[i] for automatic restart after a crash, run this from an Administrator shell:"
            Write-Host "      autostart.bat install"
            $bat = Join-Path $Root 'start-bg.bat'
            if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
                Start-Process -FilePath "$env:SystemRoot\System32\wscript.exe" -ArgumentList "`"$StartupFile`"" -WindowStyle Hidden | Out-Null
            }
        }

        Write-Host "[*] dashboard: http://localhost:$Port"
    }

    'remove' {
        $found = $false
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "[*] scheduled task '$TaskName' removed"
            $found = $true
        }
        if (Test-Path $StartupFile) {
            Remove-StartupEntry
            $found = $true
        }
        if (-not $found) {
            Write-Host "[i] nothing to remove - auto-start is already off"
        }
    }

    'status' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            Write-Host "[*] scheduled task : $TaskName ($($task.State))"
            Write-Host "    last run       : $($info.LastRunTime)  result=$($info.LastTaskResult)"
            Write-Host "    next run       : $($info.NextRunTime)"
        } else {
            Write-Host "[i] scheduled task : not installed"
        }

        if (Test-Path $StartupFile) {
            Write-Host "[*] startup entry  : $StartupFile"
        } else {
            Write-Host "[i] startup entry  : not installed"
        }

        $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($listening) {
            Write-Host "[*] port $Port      : listening (pid $($listening[0].OwningProcess))"
        } else {
            Write-Host "[i] port $Port      : nothing is listening"
        }
    }
}
