# Running A.C.E.S. on a Linux server

This folder runs the A.C.E.S. MCP server in Docker on a Linux host, such as the AI Lab server. It's the Linux counterpart to `windows-service/`: same server, same settings, packaged as a container.

| File | What it does |
|---|---|
| `install.sh` | First-time setup. Safe to run again. |
| `update.sh` | Builds and restarts, applies a settings change, or rolls back. |
| `docker-compose.yml` | How the container runs: port, folders, restarts, health check, limits. |
| `aces.env.example` | Settings template. `install.sh` copies it to `/etc/aces/aces.env`. |

Where things live on the host:

| Path | What | Who can read it |
|---|---|---|
| `/opt/aces-mcp-server` | This repo | Whoever cloned it |
| `/etc/aces/aces.env` | Settings and secrets | root only |
| `/etc/aces/tls/` | HTTPS certificate and key | The key: the container user only (uid 10001) |
| `/var/lib/aces/` | Cache, logs, sign-in signing key | The container user only |

## Before you start

- [ ] A Linux host with Docker Engine and the Compose plugin (`docker compose version` works)
- [ ] The host can reach the SQL Server on port 1433 (ISG opens this)
- [ ] A DNS name for the server, and its certificate as a .pfx file or PEM files
- [ ] `https://<dns name>/auth/callback` added as a redirect URI on the sign-in app registration
- [ ] The database login's password, the CustomGPT API token, and the sign-in app's client secret

## First-time setup

**1. Get the code.**

```
sudo git clone https://github.com/TheCraftySlayer/aces-mcp-server.git /opt/aces-mcp-server
cd /opt/aces-mcp-server
```

If the repo is private, Git asks for a username and password. Use a fine-grained token with read-only access to this one repo as the password. Don't put the token in the URL, because Git saves the URL in plain text in `.git/config`.

**2. Run the installer once.**

```
sudo linux/install.sh
```

It makes the folders and `/etc/aces/aces.env`, prints a new `ACES_MCP_API_KEY` and `ACES_ADMIN_TOKEN`, and stops. Copy both keys now. Copilot Studio and CustomGPT need the MCP key; Power Automate and the admin routes need the admin token. They're also in the settings file if you need them later.

**3. Fill in the settings.**

```
sudo nano /etc/aces/aces.env
```

Every setting has a comment. Replace the `<...>` placeholders. If a value contains `$`, `#` or a quote, wrap it in single quotes.

**4. Put the certificate in place** as `/etc/aces/tls/aces-cert.pem` and `/etc/aces/tls/aces-key.pem`. From a .pfx file:

```
sudo openssl pkcs12 -in aces.pfx -nokeys -out /etc/aces/tls/aces-cert.pem
sudo openssl pkcs12 -in aces.pfx -nocerts -nodes -out /etc/aces/tls/aces-key.pem
```

The second command saves the key without a password; the installer then makes it readable only by the container user. To keep a password on the key, leave out `-nodes` and set `ACES_TLS_KEY_PASSWORD`.

**5. Run the installer again.**

```
sudo linux/install.sh
```

It checks the settings, the certificate and the database connection, builds the image, starts it, and should end with "A.C.E.S. is up and healthy." If something's wrong, it says what, or prints the last 40 log lines. See Troubleshooting.

**6. Check it works.**

```
curl -k https://localhost/health
```

Then sign in at `https://<dns name>/ui`, ask something that uses the database (like this week's call volume), and check that a new row landed in `ai_api.tool_log`.

## Day to day

| To do this | Run |
|---|---|
| Update to the latest code | `sudo linux/update.sh` |
| Apply a settings change | `sudo linux/update.sh --restart` |
| Go back to an earlier build | `sudo linux/update.sh --rollback <tag>` |
| See the builds on this host | `sudo docker image ls aces-mcp` |
| Check it's running | `sudo docker ps --filter name=aces` |
| Watch the log | `sudo docker logs -f aces` |
| Find older logs | `sudo ls /var/lib/aces/logs` |
| Stop or start it | `sudo docker stop aces` / `sudo docker start aces` |

The container restarts on its own after a crash or a reboot. Log files start fresh at midnight and are kept for `ACES_LOG_KEEP_DAYS` days. Old builds pile up over time; remove ones you won't roll back to with `sudo docker image rm aces-mcp:<tag>`.

When the database password changes, update `ACES_SQL_PASSWORD` and run `sudo linux/update.sh --restart`. Until you do, the database tools fail with "Login failed."

## Backups

Keep a copy of `/etc/aces` (settings, secrets, certificate) somewhere only admins can read. Nothing in `/var/lib/aces` is precious: if it's lost, the server makes a new signing key and everyone signs in again.

## Troubleshooting

| What you see | Likely cause | Fix |
|---|---|---|
| Installer warns it can't reach the SQL Server on 1433 | Firewall, or the name doesn't resolve on this host | `getent hosts <server>`; ask ISG to open 1433 from this host |
| The host reaches the database, but tools time out | The container can't resolve the short server name | Use the full DNS name in `ACES_SQL_SERVER`, then `--restart` |
| "Login failed for user" in the log | Wrong or rotated password | Fix `ACES_SQL_PASSWORD`, then `--restart` |
| Keeps restarting; log shows "Permission denied" under `/data` | Owner of `/var/lib/aces` changed | `sudo chown -R 10001:10001 /var/lib/aces && sudo chmod 700 /var/lib/aces` |
| Keeps restarting; log mentions the TLS key | The container user can't read the key | Run `sudo linux/install.sh` again; it fixes the owner |
| "Read-only file system" in the log | Something tried to write outside `/data` and `/tmp` | Report it; as a stopgap, comment out `read_only: true` in `docker-compose.yml` |
| Status stays "unhealthy" but the site works over http | HTTPS isn't set up | Change `https` to `http` in the healthcheck in `docker-compose.yml` |
| Build fails installing `msodbcsql18` | No internet to packages.microsoft.com, or the host isn't x86_64 (`uname -m`) | Allow that site, or build on an x86_64 host |
| "port is already allocated" | Something else uses 443 | Change `"443:8000"` to another port, and update the DNS name and redirect URI to match |
| Everyone signs in again after each restart | The signing key isn't kept | Check `/var/lib/aces/server-secret.key` exists and the volume is mounted |
| Sign-in error AADSTS50011 | Redirect URI missing from the app registration | Add `https://<dns name>/auth/callback` |

## How this differs from the Windows service

- **Secrets** are in a root-only file instead of being encrypted with DPAPI. Anyone in the `docker` group can read them with `docker inspect`, so keep that group to admins.
- **Database sign-in** uses a SQL login and password instead of a Windows service account. Kerberos is possible later if a stored password isn't acceptable.
- **The container is locked down.** It runs as an unprivileged user, with a read-only file system and no extra Linux privileges. It can write only to `/var/lib/aces` and a temporary folder.

## Who to ask

| Problem | Who |
|---|---|
| Database login, password or permissions | The DBA for the SQL Server |
| Network, DNS, firewall or certificate | ISG |
| The sign-in app registration | The county SSO admin |
| The server itself | The A.C.E.S. project lead |
