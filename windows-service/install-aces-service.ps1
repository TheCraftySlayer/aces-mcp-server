# Sets up the A.C.E.S. server on a county Windows server, running as a service account.
# Run it in an administrator PowerShell window from this folder. It's safe to run again.
#
#   .\install-aces-service.ps1 -ServiceAccount "DOMAIN\svc-aces$"        first install, or after an update
#   .\install-aces-service.ps1 -ServiceAccount "DOMAIN\svc-aces$" -UpdateSecrets   change a secret
#   .\install-aces-service.ps1 -Uninstall                                 remove the task and firewall rule
#
# -ServiceAccount: a group managed service account (ends in $, no password to keep) is best.
#   A regular domain account works too; it asks for that account's password once.
#   Either way the account needs "Log on as a batch job" on this server, and a database login
#   from sql/56_SERVICE_ACCOUNT_LOGIN.sql.
# -Wheelhouse: a folder of package files, for a server that can't reach the internet. Make it
#   on a PC that can: python -m pip download -r requirements.txt -d wheelhouse --platform win_amd64
#   --python-version 3.12 --only-binary=:all:
#
# What it does: checks Python and the ODBC driver, makes the data folder (default
# C:\ProgramData\ACES) readable only by administrators and the service account, builds the
# Python environment in this repo's .venv folder, copies the settings template the first time,
# asks for the secrets and keeps them encrypted for this machine, registers the "A.C.E.S. Server"
# task to start at boot, opens the port on the domain firewall profile, and starts the server.
# docs/OPERATIONS.md has the whole procedure.

param(
    [string]$ServiceAccount = "",
    [string]$DataFolder = (Join-Path $env:ProgramData "ACES"),
    [string]$Wheelhouse = "",
    [switch]$UpdateSecrets,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName     = "A.C.E.S. Server"
$FirewallName = "A.C.E.S. server"
$AppFolder    = Split-Path -Path $PSScriptRoot -Parent
$settingsFile = Join-Path $DataFolder "service-settings.env"
$secretsDir   = Join-Path $DataFolder "secrets"
$secretsFile  = Join-Path $secretsDir "service-secrets.dat"
$entropy      = [Text.Encoding]::UTF8.GetBytes("ACES service secrets v1")
# Well-known SIDs, so this works whatever language Windows is in.
$SystemSid = "*S-1-5-18"
$AdminsSid = "*S-1-5-32-544"

function Write-Step([string]$text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Write-Ok([string]$text)   { Write-Host "   OK  $text" -ForegroundColor Green }
function Write-Note([string]$text) { Write-Host "   $text" -ForegroundColor Yellow }
function Stop-Install([string]$text) { Write-Host ""; Write-Host $text -ForegroundColor Red; exit 1 }

function Invoke-Checked([string]$what, [scriptblock]$command) {
    & $command
    if ($LASTEXITCODE -ne 0) { Stop-Install "$what failed (exit code $LASTEXITCODE)." }
}

function Import-DataProtection {
    foreach ($assembly in @("System.Security", "System.Security.Cryptography.ProtectedData")) {
        try { Add-Type -AssemblyName $assembly -ErrorAction Stop } catch { }
    }
    if (-not ("System.Security.Cryptography.ProtectedData" -as [type])) {
        Stop-Install "Windows data protection (DPAPI) isn't available in this PowerShell. Use Windows PowerShell 5.1."
    }
}

function New-RandomKey {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes) -replace '[+/=]', '')
}

function Read-Secrets {
    if (-not (Test-Path $secretsFile)) { return @{} }
    Import-DataProtection
    $plain = [Security.Cryptography.ProtectedData]::Unprotect(
        [IO.File]::ReadAllBytes($secretsFile), $entropy, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    $values = @{}
    $parsed = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
    foreach ($property in $parsed.PSObject.Properties) { $values[$property.Name] = [string]$property.Value }
    return $values
}

function Save-Secrets([hashtable]$values) {
    Import-DataProtection
    $bytes = [Text.Encoding]::UTF8.GetBytes(($values | ConvertTo-Json -Compress))
    $protected = [Security.Cryptography.ProtectedData]::Protect(
        $bytes, $entropy, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    [IO.File]::WriteAllBytes($secretsFile, $protected)
}

function Read-Plain([string]$prompt) {
    $secure = Read-Host -Prompt $prompt -AsSecureString
    if ($null -eq $secure -or $secure.Length -eq 0) { return "" }
    return [Net.NetworkCredential]::new("", $secure).Password
}

function Get-SettingValue([string]$name, [string]$fallback) {
    if (-not (Test-Path $settingsFile)) { return $fallback }
    foreach ($line in Get-Content -Path $settingsFile -Encoding UTF8) {
        $text = $line.Trim()
        if ($text.StartsWith("#")) { continue }
        $key, $value = $text -split "=", 2
        if ($key -and $key.Trim() -eq $name -and $value) { return $value.Trim() }
    }
    return $fallback
}

# ---------------------------------------------------------------------------
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Stop-Install "Run this in a PowerShell window opened with 'Run as administrator'."
}
if (-not (Test-Path (Join-Path $AppFolder "server.py"))) {
    Stop-Install "server.py isn't in $AppFolder. Run this script from the windows-service folder of the repo."
}

if ($Uninstall) {
    Write-Step "Removing the task and firewall rule"
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok "task removed"
    }
    Get-NetFirewallRule -DisplayName $FirewallName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Ok "firewall rule removed"
    Write-Note "Left in place: $DataFolder (settings, encrypted secrets, logs) and $AppFolder. Delete them by hand if you're done with them."
    exit 0
}

if (-not $ServiceAccount) { Stop-Install "Give the service account: -ServiceAccount 'DOMAIN\svc-aces$'" }
if ($ServiceAccount -notmatch '^[^\\]+\\[^\\]+$') { Stop-Install "Write the service account as DOMAIN\name." }

# ---------------------------------------------------------------------------
Write-Step "1. Python and the SQL Server ODBC driver"
$pythonExe = $null
foreach ($candidate in @(@("py", "-3.12"), @("py", "-3.11"), @("python"))) {
    $exe, $arguments = $candidate
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $found = & $exe @arguments -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
    if ($LASTEXITCODE -eq 0 -and $found) { $pythonExe = ([string]$found).Trim(); break }
}
if (-not $pythonExe) { Stop-Install "Python 3.11 or later isn't installed. Install Python 3.12 for all users, then run this again." }
Write-Ok "Python: $pythonExe"
if (Get-OdbcDriver -Name "ODBC Driver 18 for SQL Server" -ErrorAction SilentlyContinue) {
    Write-Ok "ODBC Driver 18 for SQL Server"
} else {
    Write-Note "ODBC Driver 18 for SQL Server isn't installed. The data tools won't work until it is."
}

# ---------------------------------------------------------------------------
Write-Step "2. Folders and permissions"
foreach ($folder in @($DataFolder, (Join-Path $DataFolder "logs"), (Join-Path $DataFolder "tls"), $secretsDir)) {
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
}
# Data folder: administrators and SYSTEM in full, the service account can write (logs, caches).
Invoke-Checked "Setting permissions on $DataFolder" {
    icacls $DataFolder /inheritance:r /grant:r "${SystemSid}:(OI)(CI)F" "${AdminsSid}:(OI)(CI)F" "${ServiceAccount}:(OI)(CI)M" | Out-Null
}
# Secrets folder: the service account can read and create its signing secret, nothing more.
Invoke-Checked "Setting permissions on $secretsDir" {
    icacls $secretsDir /inheritance:r /grant:r "${SystemSid}:(OI)(CI)F" "${AdminsSid}:(OI)(CI)F" "${ServiceAccount}:(OI)(CI)(RD,RA,REA,RC,X,S,WD,AD)" | Out-Null
}
# The code folder: the service account reads and runs it, and can't change it.
Invoke-Checked "Setting permissions on $AppFolder" {
    icacls $AppFolder /grant "${ServiceAccount}:(OI)(CI)RX" | Out-Null
}
Write-Ok "$DataFolder is limited to administrators and $ServiceAccount"

# ---------------------------------------------------------------------------
Write-Step "3. Python environment"
$venvPython = Join-Path $AppFolder ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Invoke-Checked "Creating the Python environment" { & $pythonExe -m venv (Join-Path $AppFolder ".venv") }
}
$pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $AppFolder "requirements.txt"))
if ($Wheelhouse) {
    if (-not (Test-Path $Wheelhouse)) { Stop-Install "No wheelhouse folder at $Wheelhouse." }
    $pipArgs += @("--no-index", "--find-links", $Wheelhouse)
}
Invoke-Checked "Installing packages" { & $venvPython @pipArgs }
Write-Ok "packages installed from requirements.txt"

# ---------------------------------------------------------------------------
Write-Step "4. Settings"
if (-not (Test-Path $settingsFile)) {
    Copy-Item -Path (Join-Path $PSScriptRoot "service-settings.example.env") -Destination $settingsFile
    Write-Note "Created $settingsFile from the template. Fill in the server name, sign-in IDs and group, then run this again."
    Write-Note "Opening it in Notepad now."
    Start-Process notepad.exe -ArgumentList "`"$settingsFile`""
    exit 0
}
Write-Ok "using $settingsFile"

# ---------------------------------------------------------------------------
Write-Step "5. Secrets (kept encrypted for this machine, never in the settings file)"
$secrets = Read-Secrets
$prompts = [ordered]@{
    ACES_MCP_API_KEY        = "MCP access key for Copilot Studio and CustomGPT (Enter to make a new one)"
    ACES_ADMIN_TOKEN        = "Admin token for Power Automate and the admin routes (Enter to make a new one)"
    CUSTOMGPT_API_TOKEN     = "CustomGPT API token"
    ACES_OIDC_CLIENT_SECRET = "Client secret of the sign-in app registration"
    ACES_TLS_KEY_PASSWORD   = "Password of the TLS key file (Enter if the key has none)"
    AZURE_OPENAI_API_KEY    = "Azure OpenAI key (Enter to skip; only if that chat model is approved)"
    ANTHROPIC_API_KEY       = "Anthropic API key (Enter to skip; only if that chat model is approved)"
}
$changed = $false
foreach ($name in $prompts.Keys) {
    $has = [bool]$secrets[$name]
    if ($has -and -not $UpdateSecrets) { continue }
    $suffix = if ($has) { " [Enter keeps the current one]" } else { "" }
    $value = Read-Plain ($prompts[$name] + $suffix)
    if (-not $value -and -not $has -and $name -in @("ACES_MCP_API_KEY", "ACES_ADMIN_TOKEN")) {
        $value = New-RandomKey
        Write-Note "Made a new $name. Copy it now; it isn't shown again: $value"
    }
    if ($value) { $secrets[$name] = $value; $changed = $true }
}
if ($changed -or -not (Test-Path $secretsFile)) { Save-Secrets $secrets; Write-Ok "secrets saved to $secretsFile" }
else { Write-Ok "secrets unchanged" }
# Only the service account and administrators can read the secrets file.
Invoke-Checked "Setting permissions on $secretsFile" {
    icacls $secretsFile /inheritance:r /grant:r "${SystemSid}:F" "${AdminsSid}:F" "${ServiceAccount}:R" | Out-Null
}

# ---------------------------------------------------------------------------
Write-Step "6. Scheduled task"
$runner = Join-Path $PSScriptRoot "run-aces-service.ps1"
$action = New-ScheduledTaskAction `
    -Execute (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe") `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runner`" -DataFolder `"$DataFolder`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
if ($ServiceAccount.EndsWith('$')) {
    # Group managed service account: Windows fetches its password itself.
    $principal = New-ScheduledTaskPrincipal -UserId $ServiceAccount -LogonType Password -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Force | Out-Null
} else {
    $credential = Get-Credential -UserName $ServiceAccount -Message "Password for $ServiceAccount (used only to register the task)"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User $ServiceAccount -Password $credential.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
}
Write-Ok "task '$TaskName' starts at boot as $ServiceAccount"

# ---------------------------------------------------------------------------
Write-Step "7. Firewall"
$port = [int](Get-SettingValue "PORT" "10000")
if (-not (Get-NetFirewallRule -DisplayName $FirewallName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $FirewallName -Direction Inbound -Protocol TCP -LocalPort $port `
        -Action Allow -Profile Domain | Out-Null
} else {
    Set-NetFirewallRule -DisplayName $FirewallName -LocalPort $port -Profile Domain
}
Write-Ok "TCP $port open on the domain network only"

# ---------------------------------------------------------------------------
Write-Step "8. Start and check"
Start-ScheduledTask -TaskName $TaskName
$scheme = if (Get-SettingValue "ACES_TLS_CERT_FILE" "") { "https" } else { "http" }
$healthy = $false
for ($i = 0; $i -lt 30 -and -not $healthy; $i++) {
    Start-Sleep -Seconds 2
    # curl.exe -k: the certificate is issued for the server's name, not localhost.
    $body = & curl.exe -sk --max-time 3 "${scheme}://localhost:$port/health" 2>$null
    $healthy = ($LASTEXITCODE -eq 0 -and $body -match '"healthy"')
}
if ($healthy) {
    Write-Ok "the server answers on ${scheme}://localhost:$port/health"
    Write-Host ""
    Write-Host "Done. Staff open the address in ACES_PUBLIC_BASE_URL followed by /ui." -ForegroundColor Green
} else {
    Write-Note "The server didn't answer within a minute. Check $(Join-Path $DataFolder 'logs\service.log') and the newest process-*.err.log there."
    exit 1
}
