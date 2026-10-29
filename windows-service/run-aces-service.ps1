# Runs the A.C.E.S. server on a county Windows server. The "A.C.E.S. Server" scheduled task
# starts this at boot as the service account (install-aces-service.ps1 sets that up); don't
# run it by hand except to test.
#
# It loads the settings file and the encrypted secrets from the data folder, then starts
# the server and starts it again whenever it stops. Everything it does is written to
# logs\service.log in the data folder; the server's own output goes to logs\aces.log.

param([string]$DataFolder = (Join-Path $env:ProgramData "ACES"))

$ErrorActionPreference = "Stop"
$AppFolder    = Split-Path -Path $PSScriptRoot -Parent
$python       = Join-Path $AppFolder ".venv\Scripts\python.exe"
$logDir       = Join-Path $DataFolder "logs"
$settingsFile = Join-Path $DataFolder "service-settings.env"
$secretsFile  = Join-Path $DataFolder "secrets\service-secrets.dat"
$entropy      = [Text.Encoding]::UTF8.GetBytes("ACES service secrets v1")

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Write-ServiceLog([string]$message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"), $message
    Add-Content -Path (Join-Path $logDir "service.log") -Value $line -Encoding UTF8
}

function Import-DataProtection {
    foreach ($assembly in @("System.Security", "System.Security.Cryptography.ProtectedData")) {
        try { Add-Type -AssemblyName $assembly -ErrorAction Stop } catch { }
    }
    if (-not ("System.Security.Cryptography.ProtectedData" -as [type])) {
        throw "Windows data protection (DPAPI) isn't available in this PowerShell."
    }
}

try {
    if (-not (Test-Path $python))       { throw "No Python environment at $python. Run install-aces-service.ps1." }
    if (-not (Test-Path $settingsFile)) { throw "No settings file at $settingsFile. Run install-aces-service.ps1." }
    if (-not (Test-Path $secretsFile))  { throw "No secrets file at $secretsFile. Run install-aces-service.ps1." }

    # Settings: KEY=value lines; blank lines and # comments skipped.
    foreach ($line in Get-Content -Path $settingsFile -Encoding UTF8) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith("#")) { continue }
        $name, $value = $text -split "=", 2
        if ($name -and $null -ne $value) {
            [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
        }
    }

    # Secrets: encrypted for this machine (DPAPI), readable only by the service account and administrators.
    Import-DataProtection
    $plain = [Security.Cryptography.ProtectedData]::Unprotect(
        [IO.File]::ReadAllBytes($secretsFile), $entropy, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    $secrets = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
    foreach ($property in $secrets.PSObject.Properties) {
        if ($property.Value) { [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, "Process") }
    }

    # Files the server writes, kept in the data folder unless the settings say otherwise.
    $defaults = @{
        ACES_LOG_FILE             = Join-Path $logDir "aces.log"
        TASK_CACHE_FILE           = Join-Path $DataFolder "aces_task_cache.json"
        ACES_FILE_LINK_CACHE_FILE = Join-Path $DataFolder "aces_file_links.json"
        ACES_SERVER_SECRET_FILE   = Join-Path $DataFolder "secrets\server-secret.key"
    }
    foreach ($name in $defaults.Keys) {
        if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
            [Environment]::SetEnvironmentVariable($name, $defaults[$name], "Process")
        }
    }
    $port = [int]$(if ($env:PORT) { $env:PORT } else { "10000" })
    $keepDays = [int]$(if ($env:ACES_LOG_KEEP_DAYS) { $env:ACES_LOG_KEEP_DAYS } else { "90" })
} catch {
    Write-ServiceLog "NOT STARTED: $($_.Exception.Message)"
    exit 1
}

Write-ServiceLog "service starting as $([Security.Principal.WindowsIdentity]::GetCurrent().Name), port $port"

while ($true) {
    # A server left over from an earlier run of this task (stopping the task can leave it
    # running) would hold the port. Stop it, but only if it's this folder's Python.
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        if ($owner -and $owner.Path -eq $python) {
            Write-ServiceLog "stopping a leftover server (process $($owner.Id))"
            Stop-Process -Id $owner.Id -Force
            Start-Sleep -Seconds 3
        } else {
            Write-ServiceLog "port $port is in use by another program; waiting a minute"
            Start-Sleep -Seconds 60
            continue
        }
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $process = Start-Process -FilePath $python -ArgumentList "server.py" -WorkingDirectory $AppFolder `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $logDir "process-$stamp.out.log") `
        -RedirectStandardError (Join-Path $logDir "process-$stamp.err.log")
    $null = $process.Handle   # keeps the exit code readable after the process ends
    Write-ServiceLog "server started (process $($process.Id))"
    $process.WaitForExit()
    Write-ServiceLog "server stopped with exit code $($process.ExitCode); starting it again in 15 seconds"

    Get-ChildItem -Path $logDir -Filter "process-*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$keepDays) } |
        Remove-Item -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 15
}
