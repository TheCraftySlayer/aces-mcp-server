#!/usr/bin/env bash
# First-time setup for the A.C.E.S. MCP server on a Linux host. Safe to run again.
#
#   sudo linux/install.sh
#
# First run: makes the folders and the settings file (/etc/aces/aces.env), fills in new
# access keys, and stops so you can fill in the rest. Next run: checks the settings, the
# certificate and the database connection, then builds and starts the server.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
env_file=/etc/aces/aces.env
tls_dir=/etc/aces/tls
data_dir=/var/lib/aces
app_uid=10001   # the user the container runs as (see the Dockerfile)

die()  { echo "ERROR: $*" >&2; exit 1; }
note() { echo "   - $*"; }

# The value of one setting in aces.env, without surrounding quotes. Empty if it isn't set.
value_of() {
  local line
  line=$(grep -E "^$1=" "$env_file" | tail -n 1) || true
  line=${line#*=}
  if [[ ${#line} -ge 2 && ( $line == \'*\' || $line == \"*\" ) ]]; then
    line=${line:1:${#line}-2}
  fi
  printf '%s' "$line"
}
new_key() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

[[ $EUID -eq 0 ]] || die "run with sudo."
command -v docker >/dev/null || die "Docker isn't installed. Install Docker Engine first."
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin isn't installed ('docker compose' doesn't work)."

echo "1. Folders"
install -d -m 755 /etc/aces
install -d -m 755 "$tls_dir"
install -d -o "$app_uid" -g "$app_uid" -m 700 "$data_dir"
note "$data_dir holds the cache, logs and sign-in signing key (container user only)"

echo "2. Settings"
if [[ ! -f $env_file ]]; then
  install -m 600 -o root -g root "$here/aces.env.example" "$env_file"
  mcp_key=$(new_key)
  admin_token=$(new_key)
  sed -i -e "s/^ACES_MCP_API_KEY=\$/ACES_MCP_API_KEY=$mcp_key/" \
         -e "s/^ACES_ADMIN_TOKEN=\$/ACES_ADMIN_TOKEN=$admin_token/" "$env_file"
  note "made $env_file (root only) with new access keys. Copy them now:"
  echo "       ACES_MCP_API_KEY=$mcp_key"
  echo "       ACES_ADMIN_TOKEN=$admin_token"
  echo
  echo "Next: fill in the rest of $env_file (sudo nano $env_file), put the certificate"
  echo "in $tls_dir, then run sudo linux/install.sh again."
  exit 0
fi
chown root:root "$env_file"
chmod 600 "$env_file"

missing=()
for name in ACES_SQL_SERVER ACES_SQL_DATABASE ACES_SQL_USER ACES_SQL_PASSWORD ACES_MCP_API_KEY ACES_ADMIN_TOKEN; do
  [[ -n $(value_of "$name") ]] || missing+=("$name")
done
(( ${#missing[@]} == 0 )) || die "fill in these settings in $env_file: ${missing[*]}"
for name in ACES_SQL_SERVER ACES_SQL_DATABASE ACES_SQL_USER; do
  [[ $(value_of "$name") != *'<'* ]] || die "replace the <...> placeholder in $name in $env_file"
done
note "required settings are filled in"

echo "3. Certificate"
cert=$(value_of ACES_TLS_CERT_FILE)
key=$(value_of ACES_TLS_KEY_FILE)
if [[ -z $cert && -z $key ]]; then
  note "no certificate set, so the server uses plain http. Only do that behind a proxy that"
  note "handles HTTPS, and change https to http in the healthcheck in linux/docker-compose.yml."
else
  [[ $cert == /tls/* && $key == /tls/* ]] \
    || die "ACES_TLS_CERT_FILE and ACES_TLS_KEY_FILE must both point into /tls/ (that's $tls_dir on this host)"
  host_cert=$tls_dir/${cert#/tls/}
  host_key=$tls_dir/${key#/tls/}
  [[ -f $host_cert ]] || die "certificate not found: $host_cert"
  [[ -f $host_key ]] || die "key not found: $host_key"
  chown root:root "$host_cert"
  chmod 644 "$host_cert"
  chown "$app_uid:$app_uid" "$host_key"
  chmod 400 "$host_key"
  note "certificate and key in place; only the container user can read the key"
  if command -v openssl >/dev/null && ! openssl x509 -checkend $((30 * 86400)) -noout -in "$host_cert" >/dev/null 2>&1; then
    note "WARNING: the certificate has expired or expires within 30 days"
  fi
fi

echo "4. Network"
sql_host=$(value_of ACES_SQL_SERVER)
sql_port=$(value_of ACES_SQL_PORT)
sql_port=${sql_port:-1433}
if timeout 5 bash -c "exec 3<>/dev/tcp/$sql_host/$sql_port" 2>/dev/null; then
  note "this host can reach $sql_host on port $sql_port"
else
  note "WARNING: can't reach $sql_host on port $sql_port. The server will start, but the"
  note "database tools won't work until ISG opens it. Check the name: getent hosts $sql_host"
fi
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active' \
   && ! ufw status | grep -qE '^443(/tcp)?[[:space:]]+ALLOW'; then
  note "the host firewall (ufw) is on and doesn't allow 443 yet: sudo ufw allow 443/tcp"
fi
arch=$(uname -m)
[[ $arch == x86_64 ]] || note "this host is $arch, not x86_64. If the build fails installing msodbcsql18, see Troubleshooting in linux/README.md."

echo "5. Build and start"
exec bash "$here/update.sh" --no-pull
