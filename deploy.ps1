<#
  deploy.ps1 - Build, deploy and verify the A.C.E.S. MCP server on Azure App Service.

  Run from the repo folder:   .\deploy.ps1

  It stops at the first failed check, before anything reaches Azure:
    1. Key files contain code the server can't run without (catches a stale or wrong copy).
    2. requirements.txt is fully pinned.
    3. Git is committed and pushed, so the image matches GitHub.
    4. The Python files compile.
    5. The server starts locally with test settings and lists every tool.
       (This is the check that would have caught the October 15 nov_link crash.)
  Then:
    6. Builds the image in ACR, tagged with the date and git commit.
    7. Points the web app at the new image.
    8. Waits until /health reports the new build and /mcp lists every tool.
       If that doesn't happen in time, it rolls back to the previous image.

  Options:
    -SkipSmokeTest   skip step 5 (only if local Python can't run the server)
    -AllowDirty      skip step 3 (testing only; the image won't match GitHub)

  When you add something the server can't run without, add a line to $markers below.
#>
param(
    [string]$ResourceGroup = "rg-aces-mcp",
    [string]$App           = "aces-mcp-bernco",
    [string]$Registry      = "acesmcpbernco",
    [string]$Repo          = "aces-mcp",
    [int]$ExpectedTools    = 20,
    [int]$VerifyMinutes    = 8,
    [switch]$SkipSmokeTest,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Stop-Deploy([string]$Message) {
    Write-Host ""
    Write-Host "STOPPED: $Message" -ForegroundColor Red
    exit 1
}
function Write-Ok([string]$Message)   { Write-Host "  ok  $Message" -ForegroundColor Green }
function Write-Step([string]$Message) { Write-Host ""; Write-Host "== $Message" -ForegroundColor Cyan }

$listBody = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

function Get-ToolCount([string]$Url, [string]$Key) {
    $headers = @{ "x-aces-mcp-key" = $Key; "Accept" = "application/json, text/event-stream" }
    $r = Invoke-RestMethod -Method Post -Uri $Url -Headers $headers -ContentType "application/json" -Body $listBody -TimeoutSec 30
    return @($r.result.tools).Count
}

# ---------------------------------------------------------------------------
Write-Step "1. Code checks"
$markers = @(
    @{ File = "server.py";         Text = "aces_sql_tools.register_sql_tools(" },
    @{ File = "server.py";         Text = "ACES_BUILD_TAG" },
    @{ File = "aces_sql_tools.py"; Text = "nov_link: Optional" },
    @{ File = "aces_sql_tools.py"; Text = "no_outcome_calls" },
    @{ File = "Dockerfile";        Text = "ARG ACES_BUILD_TAG" }
)
foreach ($m in $markers) {
    if (-not (Test-Path $m.File)) { Stop-Deploy "$($m.File) is missing." }
    if (-not (Select-String -Path $m.File -Pattern $m.Text -SimpleMatch -Quiet)) {
        Stop-Deploy "$($m.File) doesn't contain '$($m.Text)'. Is this the right copy of the file?"
    }
}
Write-Ok "key files have the expected code"

# ---------------------------------------------------------------------------
Write-Step "2. Pinned packages"
$unpinned = @(Get-Content requirements.txt | Where-Object { $_ -match '^\s*[A-Za-z0-9]' -and $_ -notmatch '==' })
if ($unpinned.Count -gt 0) { Stop-Deploy "requirements.txt has unpinned lines: $($unpinned -join ', ')" }
Write-Ok "requirements.txt is pinned"

# ---------------------------------------------------------------------------
Write-Step "3. Git"
$commit = git rev-parse --short HEAD
if ($LASTEXITCODE -ne 0 -or -not $commit) { Stop-Deploy "this folder isn't a git repo." }
$commit = "$commit".Trim()
if ($AllowDirty) {
    $commit = "$commit-dirty"
    Write-Host "  warning: -AllowDirty, so the image may not match GitHub" -ForegroundColor Yellow
} else {
    $dirty = @(git status --porcelain --untracked-files=no)
    if ($dirty.Count -gt 0) { Stop-Deploy "uncommitted changes:`n$($dirty -join "`n")`nCommit and push first." }
    git fetch --quiet
    $ahead = git rev-list --count "@{u}..HEAD"
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "couldn't compare with GitHub. Is this branch tracking origin?" }
    if ([int]"$ahead".Trim() -gt 0) { Stop-Deploy "$("$ahead".Trim()) commit(s) not pushed. Run git push first." }
    Write-Ok "committed and pushed ($commit)"
}

# ---------------------------------------------------------------------------
Write-Step "4. Compile"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Stop-Deploy "python isn't on PATH. Install it, or run from the shell where start-aces.bat works."
}
python -m py_compile server.py aces_chat.py aces_sql_tools.py
if ($LASTEXITCODE -ne 0) { Stop-Deploy "a Python file doesn't compile (see the error above)." }
Write-Ok "Python files compile"

# ---------------------------------------------------------------------------
if ($SkipSmokeTest) {
    Write-Step "5. Local smoke test: SKIPPED"
} else {
    Write-Step "5. Local smoke test"
    $port = 18765
    # Azure-like settings with dummy values. Nothing here connects to SQL, OpenAI or CustomGPT.
    $testEnv = [ordered]@{
        HOST = "127.0.0.1"; PORT = "$port"; ACES_BUILD_TAG = "smoke-test"
        ACES_MCP_API_KEY = "smoke-test"; ACES_ENTRA_SIGNIN = "false"
        ACES_SQL_TOOLS_ENABLED = "true"; ACES_SQL_SERVER = "smoke.invalid"; ACES_SQL_AUTH = "ActiveDirectoryMsi"
        ACES_CHAT_PROVIDER = "azure_openai"; CUSTOMGPT_API_TOKEN = "smoke-test"
        TASK_CACHE_FILE = (Join-Path $env:TEMP "aces_smoke_cache.json")
        FASTMCP_STATELESS_HTTP = "true"; FASTMCP_JSON_RESPONSE = "true"
    }
    $saved = @{}
    foreach ($k in $testEnv.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
        [Environment]::SetEnvironmentVariable($k, $testEnv[$k], "Process")
    }
    $outLog = Join-Path $env:TEMP "aces_smoke_out.log"
    $errLog = Join-Path $env:TEMP "aces_smoke_err.log"
    $proc = $null; $count = 0
    try {
        $proc = Start-Process -FilePath python -ArgumentList "server.py" -PassThru -NoNewWindow `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            if ($proc.HasExited) { break }
            try { $count = Get-ToolCount "http://127.0.0.1:$port/mcp" "smoke-test"; break } catch { }
        }
    } finally {
        if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], "Process") }
    }
    if ($count -lt $ExpectedTools) {
        Write-Host "---- last lines of server output ----"
        Get-Content $errLog -Tail 25 -ErrorAction SilentlyContinue
        Get-Content $outLog -Tail 10 -ErrorAction SilentlyContinue
        Stop-Deploy "the server didn't start locally with all $ExpectedTools tools (got $count). Nothing was built."
    }
    Write-Ok "server starts and lists $count tools"
}

# ---------------------------------------------------------------------------
Write-Step "6. Build"
$previous = az webapp config show -g $ResourceGroup -n $App --query linuxFxVersion -o tsv
if ($LASTEXITCODE -ne 0 -or -not $previous) { Stop-Deploy "couldn't read the current image. Are you signed in (az login)?" }
$previous = "$previous".Trim() -replace '^DOCKER\|', ''
$tag   = "v" + (Get-Date -Format "yyyyMMdd-HHmm") + "-" + $commit
$image = "$Registry.azurecr.io/${Repo}:$tag"
Write-Host "  current: $previous"
Write-Host "  new:     $image"
az acr build -r $Registry -t "${Repo}:$tag" --build-arg "ACES_BUILD_TAG=$tag" .
if ($LASTEXITCODE -ne 0) { Stop-Deploy "the image build failed. Nothing was deployed." }
Write-Ok "image built"

# ---------------------------------------------------------------------------
Write-Step "7. Deploy"
az webapp config container set -g $ResourceGroup -n $App --container-image-name $image --output none
if ($LASTEXITCODE -ne 0) { Stop-Deploy "couldn't point the app at the new image. The old image is still running." }
Write-Ok "app pointed at $tag"

# ---------------------------------------------------------------------------
Write-Step "8. Verify (up to $VerifyMinutes minutes)"
$base = "https://$App.azurewebsites.net"
$key = az webapp config appsettings list -g $ResourceGroup -n $App --query "[?name=='ACES_MCP_API_KEY'].value | [0]" -o tsv
$deadline = (Get-Date).AddMinutes($VerifyMinutes)
$verified = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 20
    try {
        $health = Invoke-RestMethod -Uri "$base/health" -TimeoutSec 20
        if ($health.build -ne $tag) { Write-Host "  still running the old build, waiting..."; continue }
        $count = Get-ToolCount "$base/mcp" "$key".Trim()
        if ($count -ge $ExpectedTools) { $verified = $true; break }
        Write-Host "  new build is up but lists $count tools, waiting..."
    } catch {
        Write-Host "  not answering yet ($($_.Exception.Message))"
    }
}
$key = $null

if (-not $verified) {
    Write-Host "  rolling back to $previous" -ForegroundColor Yellow
    az webapp config container set -g $ResourceGroup -n $App --container-image-name $previous --output none
    Stop-Deploy ("the new build didn't come up healthy, so it was rolled back to $previous. " +
                 "Check https://$App.scm.azurewebsites.net/api/logs/docker for the reason.")
}

Write-Host ""
Write-Host "DEPLOYED $tag" -ForegroundColor Green
Write-Host "Next: refresh the MCP tool in Copilot Studio so it picks up any description changes."
