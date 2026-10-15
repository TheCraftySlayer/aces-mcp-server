# Starts the A.C.E.S. server on this PC and opens the chat in your browser.
# Run it through start-aces.bat. "start-aces.bat reset" re-enters the saved secrets.

param([string]$Mode = "")

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$port = 10000
$secretFile = Join-Path $env:LOCALAPPDATA "ACES\local-secrets.xml"

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

# --- Checks -------------------------------------------------------------------

foreach ($file in @("server.py", "aces_sql_tools.py", "aces_chat.py", "aces_charts.py", "web_ui.html", "requirements.txt")) {
    if (-not (Test-Path $file)) { Stop-WithMessage "$file is missing from $PSScriptRoot. Put this launcher in the server folder." }
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Stop-WithMessage "Python isn't on your PATH. Install Python or open a window where 'python' works."
}
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Stop-WithMessage "Something is already using port $port, probably an older server window. Close it and run this again."
}

# --- Secrets: saved once, encrypted so only your Windows account on this PC can read them ---

if ($Mode -eq "reset" -and (Test-Path $secretFile)) {
    Remove-Item $secretFile
    Write-Host "Saved secrets cleared."
}

$prompts = [ordered]@{
    AzureSecret    = "Azure client secret for aces-mcp-sql"
    AnthropicKey   = "Anthropic API key (Enter to skip; chat stays off)"
    CustomGptToken = "CustomGPT API token (Enter to skip; specialist agents stay off)"
}

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

# --- Dependencies (quick when already installed) ---------------------------------

Write-Host "Checking Python packages..."
python -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { Stop-WithMessage "pip couldn't install the requirements. See the message above." }

# --- Settings for this run --------------------------------------------------------
# Fresh access key and admin token every run; they stop working when the server stops.

$mcpKey = New-RandomKey
$env:ACES_MCP_API_KEY       = $mcpKey
$env:ACES_ADMIN_TOKEN       = New-RandomKey
$env:ACES_SQL_TOOLS_ENABLED = "true"
$env:ACES_SQL_SERVER        = "test-aces2.database.windows.net"
$env:ACES_SQL_DATABASE      = "ACES_Test"
$env:ACES_SQL_USER          = "94fcfdcb-1350-47d9-84a2-b9e1d246f74a"
$env:ACES_SQL_PASSWORD      = ConvertTo-Plain $saved.AzureSecret
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
$env:NOV_EXPLAINER_URL      = "https://func-nov-explainer-bernco.azurewebsites.net/nov"
$env:UPC_ROUTER_URL         = "https://thecraftyslayer.github.io/upcrouter/"
$env:HOST                   = "127.0.0.1"
$env:PORT                   = "$port"

# --- Start the server in its own window and wait until it answers --------------------

# Double any apostrophes so a folder like "Assessor's Office" doesn't break the quoted path.
$quotedRoot = $PSScriptRoot -replace "'", "''"
$serverCommand = "Set-Location -LiteralPath '$quotedRoot'; `$host.UI.RawUI.WindowTitle = 'A.C.E.S. server (close to stop)'; python server.py"
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
    Stop-WithMessage "The server didn't start within 45 seconds. Check the server window for the error."
}

Write-Host ""
Write-Host "Server is running." -ForegroundColor Green
Write-Host ("  SQL tools: " + $(if ($health.sql_tools_registered) { "on" } else { "off" }))
Write-Host ("  Chat:      " + $(if ($health.chat_enabled) { "on ($($health.chat_model))" } else { "off (no Anthropic key saved)" }))
Write-Host ("  Workspace: " + $(if ($health.chat_workspace_set) { $env:ACES_ANTHROPIC_WORKSPACE_ID } else { "none set (chat needs one unless your Anthropic key was made inside a workspace)" }))
Write-Host ""
Write-Host "Opening the chat in your browser. Close the server window to stop."

Start-Process "http://localhost:$port/ui#key=$mcpKey"
Start-Sleep -Seconds 2
