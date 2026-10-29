# Operating A.C.E.S.

How to set up, run, update and hand over the A.C.E.S. server. Written so someone other than
the person who built it can do every task here.

## Who does what

| Task | Needs | Who |
|---|---|---|
| Change code, merge, tag a release | Write access to the repo | Primary maintainer: Aaron Newe. Second maintainer: not named yet |
| Install, update, restart the county server | Administrator on the server | The maintainers, or IT with this document |
| Rotate secrets | Administrator on the server, plus the system each secret belongs to (table below) | The maintainers |
| Add or remove staff | Membership of the staff security group in Entra | Whoever manages that group |
| Sign reviewed facts | Write access to the repo | A maintainer, after checking the source |
| Co-sign reviewed facts | Write access to the repo, or a maintainer running the command for them | The person who owns the topic |
| Database login for the server | DBA on asbcdbbernt | The county DBA |

The repo should live in an account the county owns, not a personal one, so access survives
any one person leaving. Until it moves, the primary maintainer can add the second maintainer
as a collaborator.

## Where it runs

| Where | What for | How it starts |
|---|---|---|
| A staff PC | Testing. One person, `127.0.0.1` only, access key in the URL | `start-aces.bat` |
| A county Windows server | Staff use: county sign-in, HTTPS, runs at boot | The "A.C.E.S. Server" scheduled task (`windows-service/`) |
| Azure App Service | Earlier test deployment in a test subscription, now disabled | `deploy.ps1` |

The rest of this document is about the county server.

## Setting up the county server

### Ask for these first

1. **The server.** Domain-joined Windows Server, a DNS name staff will use (for example
   `aces.<county domain>`), and a free port (443, unless IIS or something else holds it).
2. **How Copilot Studio reaches it.** Copilot Studio runs in Microsoft's cloud and calls
   `/mcp` over HTTPS. ISG has to say how a cloud service reaches an internal server, or
   whether it can. Settle this before building anything, because the Teams agent depends on it.
3. **A service account.** A group managed service account is best (no password to keep). It
   needs "Log on as a batch job" on the server.
4. **A certificate** for the DNS name, from the county's certificate authority, with an
   exportable private key.
5. **A database login.** Send the DBA `sql/56_SERVICE_ACCOUNT_LOGIN.sql` with the service
   account's name. It gives that account `ai_api_executor` only. Ask them to send back the
   results; they should show one schema owner, one role and `0` and `0` at the end.
6. **Entra sign-in.** In the app registration the server signs in with (the existing App
   Service sign-in registration works):
   - add `https://<DNS name>/auth/callback` as a Web redirect URI
   - make a client secret and note when it expires
   - Token configuration: add a groups claim, "Groups assigned to the application"
   - Enterprise application: turn on "Assignment required" and assign the staff group
   - note the tenant ID and the application (client) ID
7. **A staff security group** in Entra for everyone who may use A.C.E.S. Its object ID goes in
   `ACES_ALLOWED_GROUPS`. The same group is the audience when the Copilot Studio agent is
   published to Teams.
8. **A decision on the chat model** for the `/ui` chat page. Until there is one, leave the
   model settings empty: the chat page says chat is off and the MCP tools still work.

### Install

1. Install Python 3.12 for all users and Microsoft ODBC Driver 18 for SQL Server.
2. Clone the repo to `C:\ACES\aces-mcp-server`.
3. Export the certificate to PEM files in `C:\ProgramData\ACES\tls\` (next section).
4. In an administrator PowerShell window:
   ```
   cd C:\ACES\aces-mcp-server\windows-service
   .\install-aces-service.ps1 -ServiceAccount "DOMAIN\svc-aces$"
   ```
   The first run creates `C:\ProgramData\ACES\service-settings.env` from the template, opens
   it in Notepad and stops. Fill in the DNS name, tenant ID, client ID and group ID.
5. Run the same command again. It asks for the secrets (Enter makes new random values for the
   MCP access key and admin token; copy them when shown), registers the task, opens the port
   on the domain firewall profile, starts the server and checks `/health`.

If the server can't reach the internet, make a wheelhouse on a PC that can and add
`-Wheelhouse <folder>`:

```
python -m pip download -r requirements.txt -d wheelhouse --platform win_amd64 --python-version 3.12 --only-binary=:all:
```

### Certificate to PEM files

With PowerShell 7.4 or later, as an administrator:

```
$cert = Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -like "*aces*" }
$key  = [Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($cert)
$cert.ExportCertificatePem() | Set-Content C:\ProgramData\ACES\tls\aces-cert.pem
$key.ExportPkcs8PrivateKeyPem() | Set-Content C:\ProgramData\ACES\tls\aces-key.pem
```

Or from a `.pfx` file with OpenSSL:

```
openssl pkcs12 -in aces.pfx -nokeys -out aces-cert.pem
openssl pkcs12 -in aces.pfx -nocerts -nodes -out aces-key.pem
```

If the certificate was issued by an intermediate CA, append the intermediate certificate to
`aces-cert.pem` (OpenSSL does this when the `.pfx` contains it). The key file is readable only
by administrators and the service account because of the data folder's permissions.

### Check it

1. From a staff PC, open `https://<DNS name>/ui`. It should go to the Microsoft sign-in and
   land in the chat page with "Signed in as ..." at the top.
2. Sign in as someone outside the staff group: they should see the access-list page.
3. `curl.exe -s https://<DNS name>/health/details -H "x-aces-admin-token: <admin token>"`
   should show `"server_signin_active": true`, `"sql_tools_registered": true` and 21 tools.
4. Ask the chat (or Copilot Studio) for this week's call volume. Then ask the DBA, or check
   yourself, that the newest row in `ai_api.tool_log` has the service account as `caller`.

## Start, stop, restart

```
Stop-ScheduledTask  -TaskName "A.C.E.S. Server"
Start-ScheduledTask -TaskName "A.C.E.S. Server"
```

Stopping the task can leave the server process running; starting the task again stops that
leftover first. To stop the server completely, stop the task, then end the `python.exe` that
runs from `C:\ACES\aces-mcp-server\.venv`. The task starts the server at boot and starts it
again within 15 seconds if it stops.

## Updating

1. Merge to `main` only when the Tests workflow is green, and tag the release.
2. On the server: `cd C:\ACES\aces-mcp-server`, `git pull`, then rerun
   `windows-service\install-aces-service.ps1 -ServiceAccount "DOMAIN\svc-aces$"`. It installs
   any changed packages, keeps the settings and secrets, and restarts the server.
3. In Copilot Studio, refresh the MCP tool so it picks up changed tool descriptions. If
   `copilot/agent-instructions.md` changed, paste it into the agent's instructions too.

To go back a release, `git checkout <previous tag>` and rerun the install script.

## Logs

All in `C:\ProgramData\ACES\logs\`:

| File | What's in it |
|---|---|
| `aces.log` | The server's log: tool calls and timings, chat questions by sign-in name, sign-ins, errors. A new file each midnight, kept for `ACES_LOG_KEEP_DAYS` (default 90). Never prompts or answers. |
| `service.log` | When the server started and stopped, and why it didn't start. |
| `process-*.err.log`, `process-*.out.log` | The server's raw output for each start, including crash details. |

Set `ACES_LOG_KEEP_DAYS` to the county's retention for these records. The database keeps its
own record of every data tool call in `ai_api.tool_log`.

## Secrets

All are kept encrypted in `C:\ProgramData\ACES\secrets\service-secrets.dat` (Windows DPAPI for
this machine; the file is readable only by administrators and the service account). To change
any of them, rerun the install script with `-UpdateSecrets`; Enter keeps a value as it is.

| Secret | Also set in | Rotate when |
|---|---|---|
| `ACES_MCP_API_KEY` | The Copilot Studio MCP connection (`x-aces-mcp-key`) and CustomGPT | Someone who had it leaves, or yearly |
| `ACES_ADMIN_TOKEN` | Power Automate HTTP actions (`x-aces-admin-token`) | Same |
| `CUSTOMGPT_API_TOKEN` | The CustomGPT account | Same |
| `ACES_OIDC_CLIENT_SECRET` | The Entra app registration | Before it expires: make the new one, update here, restart, then delete the old one |
| TLS certificate and key (files, not in the secrets file) | The county CA | Before it expires: export the new PEM files over the old ones, restart |
| Server signing secret (`secrets\server-secret.key`) | Nowhere else | To sign everyone out at once: delete it and restart. Open chat pages lose their earlier tool results and fetch them again |

The database needs no secret: the server signs in as its service account.

## Staff access

Add or remove people in the staff security group. A removed person keeps access until their
A.C.E.S. sign-in runs out (8 hours, `ACES_SESSION_HOURS`); to cut everyone off sooner, delete
the server signing secret and restart. `ACES_ALLOWED_USERS` in the settings file is for named
exceptions only.

## Reviewed facts

`aces_facts.json` holds the figures, deadlines and citations the chat checks answers against.

- **Sign** a row after checking it against its source: `python sign_facts.py "Your Name" <ids>`.
- **Co-sign**: the person who owns the topic checks the row and co-signs it:
  `python sign_facts.py --cosign "Their Name" <ids>`. It has to be someone other than the
  reviewer, and a co-signature lapses if the row changes.
- **Open rows** are questions the office hasn't settled. Get the answer from the topic owner,
  rewrite the row as a fact with its source, set it to `draft`, then sign it.
- **Each January**, add the new tax year's figures (value freeze income limit and the like) as
  they're published. Until a figure is on file, the chat says it isn't confirmed.
- `python sign_facts.py --list` shows where every row stands.
- Once every reviewed row is co-signed, set `ACES_FACTS_REQUIRE_COSIGN=true` so an unco-signed
  row no longer counts.

Commit every change to the file; the commit history is the review record.

## Evaluation runs

1. Set `ACES_ANSWER_CACHE_HOURS=0` in the settings file and restart, so every question reaches
   the specialist instead of a cached answer.
2. In Copilot Studio, refresh the MCP tool, then run the evaluation sets (facts, legal limits,
   injection, data tools) and the manual checks.
3. Keep the results with the date, the server's build (from `/health`) and the agent version.
4. Set `ACES_ANSWER_CACHE_HOURS` back (default 24) and restart.

## Package advisories

The Tests workflow checks every package the server installs against published advisories on
every push and every Monday, and fails when one is found. To fix one: raise that package's pin
in `requirements.txt` to the fixed version, run the tests, merge, then update the server.

## When something's wrong

| What you see | Check |
|---|---|
| The site doesn't load at all | `service.log`: is the server starting? Is something else on the port? Is the task running? |
| `service.log` says NOT STARTED | The reason is on that line: usually a missing settings file, secrets file or Python environment. Rerun the install script |
| Sign-in shows "Sign-in didn't work" | The reason is on the page and in `aces.log`. Common ones: redirect URI not registered, client secret expired, server clock wrong |
| Someone gets the access-list page | They're not in the staff group, or the app registration isn't sending groups |
| Data tools missing from the tool list | `/health/details`: `sql_tools_registered`. Then the ODBC driver and the DBA's login |
| Data tools fail with a permission error | The schema owners differ (the check in `sql/56`), so the limited login can't run the procedures |
| Chat page says chat is off | No chat model is configured. Expected until one is approved |
| Specialist answers are slow or return a Task ID | Normal for Compliance Expert and Assessment Context Expert. Ask again later, or check CustomGPT |
