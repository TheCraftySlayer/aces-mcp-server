"""
Writes sql/current/ai_api.sql: every ai_api procedure, function and view as it is in the
database right now, the columns of the ai_api tables, and every permission granted on the
ai_api schema or its objects. Commit that file, so a reviewer can see exactly what each
procedure the data tools call does, and who may run it, without database access.

Run it after any ai_api change, with a login that can see definitions (ASR_CS_Owner or
another db_owner).

    python sql/export_ai_api.py --server asbcdbbernt --database ASRCustomerService --user ASR_CS_Owner --trust-server-cert
    python sql/export_ai_api.py --server asbcdbbernt --database ASRCustomerService --windows --trust-server-cert

--trust-server-cert is only for a county server with a self-signed certificate. The
password is asked for and never stored.
"""

import argparse
import getpass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent / "current" / "ai_api.sql"

MODULES_SQL = """
SELECT o.name, o.type_desc, m.definition
  FROM sys.sql_modules m
  JOIN sys.objects o ON o.object_id = m.object_id
 WHERE o.schema_id = SCHEMA_ID(N'ai_api')
 ORDER BY o.type_desc, o.name;
"""

COLUMNS_SQL = """
SELECT t.name, c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable
  FROM sys.tables t
  JOIN sys.columns c ON c.object_id = t.object_id
  JOIN sys.types ty ON ty.user_type_id = c.user_type_id
 WHERE t.schema_id = SCHEMA_ID(N'ai_api')
 ORDER BY t.name, c.column_id;
"""

GRANTS_SQL = """
SELECT pr.name, p.permission_name, p.state_desc,
       CASE WHEN p.class = 3 THEN NULL ELSE OBJECT_NAME(p.major_id) END
  FROM sys.database_permissions p
  JOIN sys.database_principals pr ON pr.principal_id = p.grantee_principal_id
 WHERE (p.class = 3 AND p.major_id = SCHEMA_ID(N'ai_api'))
    OR (p.class = 1 AND p.minor_id = 0 AND OBJECT_SCHEMA_NAME(p.major_id) = N'ai_api')
 ORDER BY 4, 1, 2;
"""


def _type_text(type_name: str, max_length: int, precision: int, scale: int) -> str:
    if type_name in {"varchar", "char", "varbinary", "binary"}:
        return f"{type_name}({'max' if max_length == -1 else max_length})"
    if type_name in {"nvarchar", "nchar"}:
        return f"{type_name}({'max' if max_length == -1 else max_length // 2})"
    if type_name in {"decimal", "numeric"}:
        return f"{type_name}({precision}, {scale})"
    if type_name in {"datetime2", "datetimeoffset", "time"}:
        return f"{type_name}({scale})"
    return type_name


def _grant_text(grantee: str, permission: str, state: str, object_name: str | None) -> str:
    target = f"[ai_api].[{object_name}]" if object_name else "SCHEMA::[ai_api]"
    verb = "DENY" if state == "DENY" else "GRANT"
    suffix = " WITH GRANT OPTION" if state == "GRANT_WITH_GRANT_OPTION" else ""
    return f"{verb} {permission} ON {target} TO [{grantee}]{suffix};"


def render_snapshot(server: str, database: str, exported_at: datetime, modules, columns, grants) -> str:
    """The snapshot file's text. Rows are tuples in the column order of the queries above."""
    counts: dict[str, int] = {}
    for _, type_desc, _ in modules:
        counts[type_desc] = counts.get(type_desc, 0) + 1
    tables = sorted({row[0] for row in columns})
    summary = ", ".join(f"{n} {kind.lower().replace('_', ' ')}" for kind, n in sorted(counts.items()))

    out = [
        "/*",
        f"  ai_api as it is in {database} on {server}, exported {exported_at:%Y-%m-%d %H:%M} UTC",
        "  by sql/export_ai_api.py. Generated: don't edit by hand. Rerun the script after any",
        "  ai_api change and commit the result.",
        "",
        f"  {summary or 'no modules'}; {len(tables)} table(s).",
        "*/",
        "",
        "/* ---- Permissions on the ai_api schema and its objects ---- */",
    ]
    out += [_grant_text(*row) for row in grants] or ["-- none"]

    for table in tables:
        out += ["", f"/* ---- Table ai_api.{table} (columns only) ---- */"]
        for name, column, type_name, max_length, precision, scale, nullable in columns:
            if name == table:
                null_text = "NULL" if nullable else "NOT NULL"
                out.append(f"--   {column}  {_type_text(type_name, max_length, precision, scale)}  {null_text}")

    for name, type_desc, definition in modules:
        text = (definition or "").replace("\r\n", "\n").strip()
        out += ["", f"/* ---- ai_api.{name} ({type_desc.lower().replace('_', ' ')}) ---- */", text, "GO"]
    return "\n".join(out) + "\n"


def _connect(args):
    import pyodbc  # only needed when actually exporting

    parts = [
        "Driver={ODBC Driver 18 for SQL Server}",
        f"Server={args.server}",
        f"Database={args.database}",
        "Encrypt=yes",
        "TrustServerCertificate=" + ("yes" if args.trust_server_cert else "no"),
    ]
    if args.windows:
        parts.append("Trusted_Connection=yes")
    else:
        password = getpass.getpass(f"Password for {args.user} on {args.server}: ")
        parts += [f"UID={args.user}", "PWD={" + password.replace("}", "}}") + "}"]
    return pyodbc.connect(";".join(parts) + ";", timeout=30)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the ai_api schema to sql/current/ai_api.sql.")
    parser.add_argument("--server", required=True)
    parser.add_argument("--database", required=True)
    login = parser.add_mutually_exclusive_group(required=True)
    login.add_argument("--user", help="SQL login (the password is asked for)")
    login.add_argument("--windows", action="store_true", help="sign in as the current Windows account")
    parser.add_argument("--trust-server-cert", action="store_true", help="county server with a self-signed certificate")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    conn = _connect(args)
    try:
        cursor = conn.cursor()
        modules = [tuple(row) for row in cursor.execute(MODULES_SQL).fetchall()]
        columns = [tuple(row) for row in cursor.execute(COLUMNS_SQL).fetchall()]
        grants = [tuple(row) for row in cursor.execute(GRANTS_SQL).fetchall()]
    finally:
        conn.close()

    if not modules:
        raise SystemExit("No ai_api definitions came back. Does this login have VIEW DEFINITION?")
    text = render_snapshot(args.server, args.database, datetime.now(UTC), modules, columns, grants)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {args.out} ({len(modules)} definitions, {len(grants)} permissions).")


if __name__ == "__main__":
    main()
