# Starts the A.C.E.S. server on this PC and opens the chat in your browser.
# Run it through start-aces.bat (double-click it, or the desktop shortcut).
#   start-aces.bat            county database (ASRCustomerService on asbcdbbernt)
#   start-aces.bat azure      Azure ACES_Test database instead
#   start-aces.bat shortcut   also puts an "A.C.E.S. Server" shortcut on your desktop
#   start-aces.bat reset      re-enter the saved secrets
# Options combine, e.g. start-aces.bat azure reset. "onprem" still works; it's the default now.
# If the server is already running, this restarts it.

param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Options = @())

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Which database a plain double-click uses: "onprem" or "azure".
# The test Azure subscription is off for now, so the county database is the default.
$defaultTarget = "onprem"

$port = 10000
$secretFile = Join-Path $env:LOCALAPPDATA "ACES\local-secrets.xml"
# Records which requirements.txt and Python the packages were last installed for,
# so pip only runs when one of them changes.
$packagesStamp = Join-Path $env:LOCALAPPDATA "ACES\packages-stamp.txt"

# County on-prem database, used with "start-aces.bat onprem".
# The server should sign in as ACES_MCP_Reader, which can only EXECUTE the ai_api
# procedures (sql/55 and sql/56 create it and check it). Until the DBA has created that
# login, this stays on ASR_CS_User, which can read and change every table, and the
# launcher warns about it on every on-prem start. To switch: set the line below to
# "ACES_MCP_Reader", then run "start-aces.bat reset" and enter that login's password.
$onPremSqlServer   = "asbcdbbernt"
$onPremSqlDatabase = "ASRCustomerService"
$onPremSqlUser     = "ASR_CS_User"
$limitedSqlUser    = "ACES_MCP_Reader"

function New-RandomKey {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes) -replace '[+/=]', '')
}

function ConvertTo-Plain([securestring]$secure) {
    if ($null -eq $secure -or $secure.Length -eq 0) { return "" }
    return [System.Net.NetworkCredential]::new("", $secure).Password
}

# Reads KEY=value from the .env file in this folder. Returns @(name, value) or $null.
function Get-DotEnvValue([string[]]$names) {
    $envFile = Join-Path $PSScriptRoot ".env"
    if (-not (Test-Path $envFile)) { return $null }
    $pattern = '^\s*(?:export\s+)?(' + (($names | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')\s*=\s*(.+?)\s*$'
    foreach ($line in Get-Content $envFile) {
        if ($line -match $pattern) {
            $value = $Matches[2].Trim('"').Trim("'")
            if ($value) { return @($Matches[1], $value) }
        }
    }
    return $null
}

function Stop-WithMessage([string]$message) {
    Write-Host ""
    Write-Host $message -ForegroundColor Yellow
    exit 1
}

# --- Options ------------------------------------------------------------------

$Options  = @($Options | Where-Object { $_ } | ForEach-Object { $_.ToLowerInvariant() })
$reset    = $Options -contains "reset"
$shortcut = $Options -contains "shortcut"
$unknown  = @($Options | Where-Object { $_ -notin @("reset", "onprem", "azure", "shortcut") })
if ($unknown.Count -gt 0) {
    Stop-WithMessage "Unknown option: $($unknown -join ', '). Use azure, shortcut, reset, or leave it blank for the county database."
}
if (($Options -contains "onprem") -and ($Options -contains "azure")) {
    Stop-WithMessage "Pick one database: onprem or azure, not both."
}
$onPrem = if ($Options -contains "azure") { $false } elseif ($Options -contains "onprem") { $true } else { $defaultTarget -eq "onprem" }
$target = if ($onPrem) { "county on-prem ($onPremSqlDatabase on $onPremSqlServer)" } else { "Azure (ACES_Test on test-aces2)" }

# --- Desktop shortcut (start-aces.bat shortcut) ------------------------------------

if ($shortcut) {
    $shortcutName = if ($onPrem) { "A.C.E.S. Server" } else { "A.C.E.S. Server (Azure)" }
    $shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "$shortcutName.lnk"
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    $link.TargetPath       = Join-Path $PSScriptRoot "start-aces.bat"
    $link.Arguments        = $(if ($onPrem) { "" } else { "azure" })
    $link.WorkingDirectory = $PSScriptRoot
    $link.Description      = "Start the A.C.E.S. server and open the chat"
    $link.Save()
    Write-Host "Put '$shortcutName' on your desktop. It points at this folder, so run 'start-aces.bat shortcut' again if you move the folder."
}

# --- Checks -------------------------------------------------------------------

$neededFiles = @(
    "server.py", "aces_common.py", "aces_access.py", "aces_arcgis.py", "aces_routing.py", "aces_customgpt.py",
    "aces_rest.py", "aces_sql_tools.py", "aces_chat.py", "aces_charts.py", "aces_facts.py", "aces_facts.json",
    "web_ui.html", "requirements.txt"
)
foreach ($file in $neededFiles) {
    if (-not (Test-Path $file)) { Stop-WithMessage "$file is missing from $PSScriptRoot. Put this launcher in the server folder." }
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Stop-WithMessage "Python isn't on your PATH. Install Python or open a window where 'python' works."
}

# Already running from an earlier start? Stop that server and its window so this run starts fresh
# (each run gets a new access key, so an old server can't be reopened anyway).
# Anything else on the port is left alone.
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    $isAces = $false
    try { $isAces = ((Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 2).service -eq "aces-mcp-server") } catch { }
    if (-not $isAces -and $owner -and $owner.CommandLine -match 'server\.py') { $isAces = $true }
    if (-not $isAces) {
        $ownerName = if ($owner) { $owner.Name } else { "process $($listener.OwningProcess)" }
        Stop-WithMessage "Port $port is in use by $ownerName, which isn't A.C.E.S. Close it and run this again."
    }

    Write-Host "A.C.E.S. is already running. Restarting it..."
    $toStop = @($listener.OwningProcess)
    # Walk up to the server window (powershell running "python server.py"), stopping at anything else.
    $parentId = if ($owner) { $owner.ParentProcessId } else { 0 }
    for ($i = 0; $i -lt 3 -and $parentId; $i++) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $parentId" -ErrorAction SilentlyContinue
        if (-not $parent -or $parent.CommandLine -notmatch 'server\.py') { break }
        $toStop += $parent.ProcessId
        $parentId = $parent.ParentProcessId
    }
    foreach ($id in $toStop) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }

    $freed = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) { $freed = $true; break }
    }
    if (-not $freed) { Stop-WithMessage "The old server didn't stop. Close its window and run this again." }
}

# --- Secrets: saved once, encrypted so only your Windows account on this PC can read them ---

if ($reset -and (Test-Path $secretFile)) {
    Remove-Item $secretFile
    Write-Host "Saved secrets cleared."
}

# Only the database secret for the chosen target is asked for.
$prompts = [ordered]@{}
if ($onPrem) {
    $prompts["OnPremSqlPassword"] = "Password for $onPremSqlUser on $onPremSqlServer"
} else {
    $prompts["AzureSecret"] = "Azure client secret for aces-mcp-sql"
}
$prompts["AnthropicKey"]   = "Anthropic API key (Enter to skip; chat stays off)"
$prompts["CustomGptToken"] = "CustomGPT API token (Enter to skip; specialist agents stay off)"

$saved = $null
if (Test-Path $secretFile) { $saved = Import-Clixml $secretFile }
if ($null -eq $saved) { $saved = [pscustomobject]@{} }

$changed = $false

# If the CustomGPT token isn't saved yet but a .env file in this folder has it, import it once.
if (-not ($saved.PSObject.Properties.Name -contains "CustomGptToken")) {
    $found = Get-DotEnvValue @("CUSTOMGPT_API_TOKEN", "CUSTOMGPT_API_KEY", "CUSTOM_GPT_API_KEY")
    if ($found) {
        $secure = ConvertTo-SecureString $found[1] -AsPlainText -Force
        $saved | Add-Member -NotePropertyName CustomGptToken -NotePropertyValue $secure -Force
        $changed = $true
        Write-Host "Imported the CustomGPT token from .env ($($found[0])) into the encrypted store."
        Write-Host "It's now saved encrypted, so you can delete that line from .env." -ForegroundColor Yellow
    }
}

# The Anthropic workspace ID isn't a secret, but it's kept in the same store so it
# survives a fresh copy of this folder. .env wins when it has one; otherwise use the saved one.
$workspaceFound = Get-DotEnvValue @("ACES_ANTHROPIC_WORKSPACE_ID", "ANTHROPIC_WORKSPACE_ID")
if ($workspaceFound -and $saved.AnthropicWorkspaceId -ne $workspaceFound[1]) {
    $saved | Add-Member -NotePropertyName AnthropicWorkspaceId -NotePropertyValue $workspaceFound[1] -Force
    $changed = $true
    Write-Host "Saved the Anthropic workspace ID from .env."
}

# No ID saved and none in .env: ask for it, but only when an Anthropic key is saved.
# Keys made inside a workspace don't need one, so Enter skips (it asks again next run).
$hasAnthropicKey = ($saved.AnthropicKey -is [securestring]) -and ($saved.AnthropicKey.Length -gt 0)
if ($hasAnthropicKey -and -not $saved.AnthropicWorkspaceId) {
    Write-Host ""
    Write-Host "Anthropic workspace ID: find it in the Anthropic Console under Settings > Workspaces (starts with wrkspc_)."
    $entered = (Read-Host "Workspace ID (Enter to skip if your key was made inside a workspace)").Trim()
    if ($entered -match '^wrkspc_[A-Za-z0-9]+$') {
        $saved | Add-Member -NotePropertyName AnthropicWorkspaceId -NotePropertyValue $entered -Force
        $changed = $true
    } elseif ($entered) {
        Write-Host "That doesn't look like a workspace ID (wrkspc_...). Not saved; you'll be asked again next run." -ForegroundColor Yellow
    }
}

# Ask only for secrets that still aren't saved (first run, or a secret added in a newer version of this script).
$missing = @($prompts.Keys | Where-Object { -not ($saved.PSObject.Properties.Name -contains $_) })
if ($missing.Count -gt 0) {
    Write-Host "Enter these once; they're saved encrypted for your Windows account only."
    Write-Host "Nothing shows while you paste. Press Enter after each one."
    foreach ($name in $missing) {
        $value = Read-Host $prompts[$name] -AsSecureString
        $saved | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force
    }
    $changed = $true
}

if ($changed) {
    New-Item -ItemType Directory -Force -Path (Split-Path $secretFile) | Out-Null
    $saved | Export-Clixml $secretFile
}

# --- Dependencies: pip only runs when requirements.txt or Python changed -------------

$pythonInfo = (& python -c "import sys; print(sys.executable, sys.version.split()[0])") -join " "
$wantStamp  = (Get-FileHash requirements.txt -Algorithm SHA256).Hash + " " + $pythonInfo
$haveStamp  = if (Test-Path $packagesStamp) { (Get-Content $packagesStamp -Raw).Trim() } else { "" }
if ($haveStamp -eq $wantStamp) {
    Write-Host "Python packages are up to date."
} else {
    Write-Host "Installing Python packages (only needed when requirements.txt or Python changes)..."
    python -m pip install -q --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "pip couldn't install the requirements. See the message above." }
    New-Item -ItemType Directory -Force -Path (Split-Path $packagesStamp) | Out-Null
    Set-Content -Path $packagesStamp -Value $wantStamp
}

# --- Settings for this run --------------------------------------------------------
# Fresh access key and admin token every run; they stop working when the server stops.

$mcpKey = New-RandomKey
$env:ACES_MCP_API_KEY       = $mcpKey
$adminToken = New-RandomKey
$env:ACES_ADMIN_TOKEN       = $adminToken
$env:ACES_SQL_TOOLS_ENABLED = "true"
# Every SQL setting is set for both targets so nothing left over in this window carries across.
if ($onPrem) {
    # The county server uses a self-signed certificate, so the certificate check is relaxed.
    # The connection is still encrypted.
    $env:ACES_SQL_SERVER            = $onPremSqlServer
    $env:ACES_SQL_DATABASE          = $onPremSqlDatabase
    $env:ACES_SQL_AUTH              = "SqlPassword"
    $env:ACES_SQL_USER              = $onPremSqlUser
    $env:ACES_SQL_PASSWORD          = ConvertTo-Plain $saved.OnPremSqlPassword
    $env:ACES_SQL_TRUST_SERVER_CERT = "true"
} else {
    $env:ACES_SQL_SERVER            = "test-aces2.database.windows.net"
    $env:ACES_SQL_DATABASE          = "ACES_Test"
    $env:ACES_SQL_AUTH              = "ActiveDirectoryServicePrincipal"
    $env:ACES_SQL_USER              = "94fcfdcb-1350-47d9-84a2-b9e1d246f74a"
    $env:ACES_SQL_PASSWORD          = ConvertTo-Plain $saved.AzureSecret
    $env:ACES_SQL_TRUST_SERVER_CERT = "false"
}
$env:ANTHROPIC_API_KEY      = ConvertTo-Plain $saved.AnthropicKey
# Only needed if your Anthropic key isn't tied to one workspace. Put the ID in .env once as
#   ACES_ANTHROPIC_WORKSPACE_ID=wrkspc_...
# After one run it's saved with the secrets, so a new copy of this folder doesn't need .env.
$env:ACES_ANTHROPIC_WORKSPACE_ID = $(if ($saved.AnthropicWorkspaceId) { [string]$saved.AnthropicWorkspaceId } else { "" })

# CustomGPT specialist agents (project IDs are not secrets)
$env:CUSTOMGPT_API_TOKEN    = ConvertTo-Plain $saved.CustomGptToken
$env:COMMUNITY_PROJECT_ID   = "9262"
$env:ASSESSMENT_PROJECT_ID  = "94006"
$env:CLEAR_PROJECT_ID       = "49501"
$env:COMPLIANCE_PROJECT_ID  = "37400"
$env:SMART_TASKS_PROJECT_ID = "9262"
$env:HOMEHARVEST_ACTION_ID  = "7"
# Download links for generated files should point at this PC, not Render.
$env:ACES_PUBLIC_BASE_URL   = "http://localhost:$port"
$env:UPC_ROUTER_URL         = "https://thecraftyslayer.github.io/upcrouter/"
$env:HOST                   = "127.0.0.1"
$env:PORT                   = "$port"

# --- Start the server in its own window and wait until it answers --------------------

# Double any apostrophes so a folder like "Assessor's Office" doesn't break the quoted path.
$quotedRoot = $PSScriptRoot -replace "'", "''"
$windowTitle = if ($onPrem) { "A.C.E.S. server, county database (close to stop)" } else { "A.C.E.S. server (close to stop)" }
$serverCommand = "Set-Location -LiteralPath '$quotedRoot'; `$host.UI.RawUI.WindowTitle = '$windowTitle'; python server.py"
Start-Process powershell -ArgumentList '-NoExit', '-Command', $serverCommand | Out-Null

# The secrets now live only in the server window's process.
foreach ($name in "ACES_MCP_API_KEY", "ACES_ADMIN_TOKEN", "ACES_SQL_PASSWORD", "ANTHROPIC_API_KEY", "CUSTOMGPT_API_TOKEN") {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}

Write-Host "Starting the server..."
$health = $null
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 750
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 2
        break
    } catch { }
}
if ($null -eq $health) {
    # In case a package is missing or broken, make the next run reinstall them.
    Remove-Item $packagesStamp -ErrorAction SilentlyContinue
    Stop-WithMessage "The server didn't start within 45 seconds. Check the server window for the error. The next run reinstalls the Python packages in case that's the cause."
}

# /health only says the server is up. The details (SQL tools, chat model) need the admin token.
$details = $health
try {
    $details = Invoke-RestMethod "http://127.0.0.1:$port/health/details" -Headers @{ "x-aces-admin-token" = $adminToken } -TimeoutSec 5
} catch { }
Remove-Variable adminToken

Write-Host ""
Write-Host "Server is running." -ForegroundColor Green
Write-Host ("  SQL tools: " + $(if ($details.sql_tools_registered) { "on" } else { "off" }))
Write-Host ("  Database:  " + $target)
if ($onPrem -and $onPremSqlUser -ne $limitedSqlUser) {
    Write-Host ("  Warning:   signed in to the database as $onPremSqlUser, which can change every table. " +
        "Switch to $limitedSqlUser (sql/55 and sql/56) before this runs anywhere but this PC.") -ForegroundColor Yellow
}
Write-Host ("  Chat:      " + $(if ($details.chat_enabled) { "on ($($details.chat_model))" } else { "off (no Anthropic key saved)" }))
Write-Host ("  Workspace: " + $(if ($details.chat_workspace_set) { $env:ACES_ANTHROPIC_WORKSPACE_ID } else { "none set (chat needs one unless your Anthropic key was made inside a workspace)" }))
Write-Host ""
Write-Host "Opening the chat in your browser. Close the server window to stop."

Start-Process "http://localhost:$port/ui#key=$mcpKey"
Start-Sleep -Seconds 2
