"""
A.C.E.S. read-only data tools (ACES database, ai_api schema).

Off by default. Nothing is registered unless ACES_SQL_TOOLS_ENABLED=true and
the connection settings are present, so deploying this file changes nothing
until you turn it on.

Design rules
  - Each tool calls exactly one stored procedure in ai_api, with one exception:
    ACES_Daily_Briefing runs five of the same read-only procedures (office
    volume, forecast, forecast accuracy, deed volume, assessment calendar) and
    returns them together. It adds no new SQL or permissions, and each call is
    still logged. Procedure names are constants here; user input only ever
    travels as bound parameters.
  - The database login can only EXECUTE ai_api procedures. It has no SELECT
    on any table or view, and every call is logged inside the procedure.
  - No exemptions, staff data, phone numbers, or free-form SQL.
  - ACES_NOV_Parcel and ACES_NOV_Search add NOV Explainer links when server.py
    passes a link builder (nov_link). Links are built here, not read from SQL.

Settings (environment variables)
  ACES_SQL_TOOLS_ENABLED   true to register the tools (default false)
  ACES_SQL_SERVER          e.g. test-aces2.database.windows.net
  ACES_SQL_DATABASE        default ACES_Test
  ACES_SQL_AUTH            ActiveDirectoryServicePrincipal (default), ActiveDirectoryMsi, or SqlPassword
  ACES_SQL_USER            app (client) ID for the service principal, or the SQL user name.
                           With ActiveDirectoryMsi: leave blank for a system-assigned identity,
                           or set the client ID of a user-assigned identity.
  ACES_SQL_PASSWORD        client secret, or the SQL user password (not used with ActiveDirectoryMsi)
  ACES_SQL_DRIVER          default "ODBC Driver 18 for SQL Server"
  ACES_SQL_TIMEOUT_SECONDS default 30
  ACES_SQL_RETRY_ATTEMPTS  default 3 (connection drops and timeouts only)

Requires: pip install pyodbc, plus Microsoft ODBC Driver 18 for SQL Server.
"""

import asyncio
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastmcp.exceptions import ToolError
from starlette.responses import JSONResponse

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


SQL_TOOLS_ENABLED = _env_flag("ACES_SQL_TOOLS_ENABLED", "false")
SQL_SERVER = os.getenv("ACES_SQL_SERVER", "").strip()
SQL_DATABASE = os.getenv("ACES_SQL_DATABASE", "ACES_Test").strip()
SQL_AUTH = os.getenv("ACES_SQL_AUTH", "ActiveDirectoryServicePrincipal").strip()
SQL_USER = os.getenv("ACES_SQL_USER", "").strip()
SQL_PASSWORD = os.getenv("ACES_SQL_PASSWORD", "").strip()
SQL_DRIVER = os.getenv("ACES_SQL_DRIVER", "ODBC Driver 18 for SQL Server").strip()
SQL_TIMEOUT_SECONDS = int(os.getenv("ACES_SQL_TIMEOUT_SECONDS", "30"))
SQL_RETRY_ATTEMPTS = max(1, int(os.getenv("ACES_SQL_RETRY_ATTEMPTS", "3")))
MAX_ROWS = 200

TOOL_NAMES = [
    "ACES_Office_Volume",
    "ACES_Forecast",
    "ACES_Forecast_Accuracy",
    "ACES_NOV_Parcel",
    "ACES_NOV_Search",
    "ACES_NOV_Coverage",
    "ACES_Deed_Volume",
    "ACES_Assessment_Calendar",
    "ACES_Daily_Briefing",
]

# Set by register_sql_tools() so the health route can report what is live.
REGISTERED = False

_ERROR_PREFIX = "Data tool error:"
_DATA_NOTE = (
    "Source: ACES database (read-only ai_api procedures), loaded from iasWorld extracts "
    "and office systems as of the latest load. Verify official values in iasWorld before relying on them."
)
_VOLUME_NOTE = "Source: ACES call, visitor and deed marts (read-only ai_api procedures). Counts reflect the latest data load."
_CALENDAR_NOTE = (
    "Source: ACES assessment calendar (fact_assessment_event). Expected contact impact and likely "
    "contact reasons are planning estimates, not measured results."
)

# How far back ACES_Daily_Briefing looks for the last open day (covers long holiday closures).
BRIEFING_LOOKBACK_DAYS = 10


def _today_mountain() -> date:
    """
    Today's date in America/Denver.

    On Windows, zoneinfo needs the tzdata package (in requirements.txt). Without
    it this falls back to UTC, which is already tomorrow after 5 or 6 PM local.
    """
    try:
        if ZoneInfo is not None:
            return datetime.now(ZoneInfo("America/Denver")).date()
    except Exception:
        pass
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Connection and execution
# ---------------------------------------------------------------------------

def _odbc_value(value: str) -> str:
    """Brace-quote a connection-string value so ; and } inside it are safe."""
    return "{" + value.replace("}", "}}") + "}"


def _uses_managed_identity() -> bool:
    return SQL_AUTH.lower() in {"activedirectorymsi", "activedirectorymanagedidentity"}


def _connection_string() -> str:
    parts = [
        f"Driver={_odbc_value(SQL_DRIVER)}",
        f"Server=tcp:{SQL_SERVER},1433",
        f"Database={_odbc_value(SQL_DATABASE)}",
        "Encrypt=yes",
        "TrustServerCertificate=no",
        f"Connection Timeout={SQL_TIMEOUT_SECONDS}",
    ]
    if _uses_managed_identity():
        # Azure supplies the token. UID is only for a user-assigned identity; never send a PWD.
        if SQL_USER:
            parts.append(f"UID={_odbc_value(SQL_USER)}")
    else:
        parts.append(f"UID={_odbc_value(SQL_USER)}")
        parts.append(f"PWD={_odbc_value(SQL_PASSWORD)}")
    if SQL_AUTH.lower() not in {"sqlpassword", "sql"}:
        parts.append(f"Authentication={SQL_AUTH}")
    return ";".join(parts) + ";"


def _config_problem() -> str:
    required = [("ACES_SQL_SERVER", SQL_SERVER)]
    if not _uses_managed_identity():
        required += [("ACES_SQL_USER", SQL_USER), ("ACES_SQL_PASSWORD", SQL_PASSWORD)]
    missing = [name for name, value in required if not value]
    return ", ".join(missing)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _run_proc_sync(proc: str, params: List[Tuple[str, Any]]) -> Tuple[List[str], List[List[Any]]]:
    import pyodbc  # imported lazily so the server starts without it when tools are off

    sql = f"EXEC {proc} " + ", ".join(f"@{name} = ?" for name, _ in params)
    values = [value for _, value in params]

    conn = pyodbc.connect(_connection_string(), timeout=SQL_TIMEOUT_SECONDS, autocommit=True)
    try:
        conn.timeout = SQL_TIMEOUT_SECONDS
        cursor = conn.cursor()
        cursor.execute(sql, values)
        while cursor.description is None:
            if not cursor.nextset():
                return [], []
        columns = [col[0] for col in cursor.description]
        rows = [[_jsonable(v) for v in row] for row in cursor.fetchmany(MAX_ROWS)]
        return columns, rows
    finally:
        conn.close()


def _run_proc_with_retry(proc: str, params: List[Tuple[str, Any]]) -> Tuple[List[str], List[List[Any]]]:
    """
    Run a procedure, retrying only on connection drops and timeouts.

    pyodbc pools connections, so a call after Azure's idle timeout can pick up a
    dead pooled connection. Those surface as OperationalError and are retried
    with a short backoff. Login failures (wrong secret) and SQL errors raised by
    the procedure are different error classes and fail immediately.
    """
    import pyodbc

    for attempt in range(SQL_RETRY_ATTEMPTS):
        try:
            return _run_proc_sync(proc, params)
        except pyodbc.OperationalError:
            if attempt == SQL_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def _clean_sql_error(exc: Exception) -> str:
    """Readable message for the caller; full detail stays in the server log."""
    text = str(exc)
    if "Can't open lib" in text or "Data source name not found" in text:
        return "The database driver is not installed on this server."
    if "Login timeout" in text or "TCP Provider" in text or "Cannot open server" in text:
        return "The database server could not be reached."
    if "Login failed" in text or "AADSTS" in text:
        return "Database sign-in failed."
    match = re.search(r"\[SQL Server\]([^\[]+?)(?:\s*\(\d+\)\s*\(SQL\w+\))", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"\[SQL Server\]([^\[]+)", text)
    if match:
        return match.group(1).strip()[:300]
    return "The database call failed."


# ---------------------------------------------------------------------------
# Input validation (the procedures validate again)
# ---------------------------------------------------------------------------

def _parse_date(value: Any, label: str) -> date:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be a date in YYYY-MM-DD format.")


def _parse_grain(value: Any) -> str:
    grain = str(value or "month").strip().lower()
    if grain not in {"day", "week", "month"}:
        raise ValueError("grain must be day, week, or month.")
    return grain


def _parse_tax_year(value: Any) -> Optional[int]:
    if value in (None, "", 0, "0"):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise ValueError("taxYear must be a four-digit year, or 0 for all years.")
    if year < 2000 or year > 2100:
        raise ValueError("taxYear must be a four-digit year, or 0 for all years.")
    return year


def _parse_int(value: Any, label: str, low: int, high: int, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number from {low} to {high}.")
    if not low <= number <= high:
        raise ValueError(f"{label} must be a whole number from {low} to {high}.")
    return number


def _looks_like_upc(text: str) -> bool:
    """Digits with optional spaces or dashes, at least 12 digits (Bernalillo UPCs are 18)."""
    return bool(re.fullmatch(r"[0-9\s-]{12,30}", text or "")) and len(re.sub(r"\D", "", text)) >= 12


def _parse_upc(value: Any) -> str:
    raw = str(value or "").strip()
    if re.search(r"[A-Za-z]{2,}", raw):
        raise ValueError(
            "That looks like an address, not a UPC. Use ACES_NOV_Search to find the UPC for an address, "
            "then ACES_NOV_Parcel with that UPC."
        )
    upc = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    if not 6 <= len(upc) <= 20:
        raise ValueError("upc must be the 6 to 20 character UPC printed on the notice.")
    return upc


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_rows(title: str, columns: List[str], rows: List[List[Any]], note: str, empty: str = "") -> str:
    if not rows:
        return f"{title}\n{empty or 'No rows returned.'}\n{note}"
    lines = [title, f"{len(rows)} row(s)" + (" (limit reached)" if len(rows) >= MAX_ROWS else ""), ""]
    for idx, row in enumerate(rows, start=1):
        pairs = [f"{col}: {'' if val is None else val}" for col, val in zip(columns, row)]
        lines.append(f"{idx}. " + " | ".join(pairs))
    lines.extend(["", note])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_sql_tools(
    mcp: Any,
    log: Callable[..., None],
    require_rest_auth: Callable[[Any], Optional[JSONResponse]],
    normalize_address: Optional[Callable[[str], str]] = None,
    nov_link: Optional[Callable[[str], str]] = None,
) -> bool:
    """Register the data tools and the /data-tool REST route. Returns True if registered."""
    global REGISTERED

    if not SQL_TOOLS_ENABLED:
        log("SQL data tools disabled (ACES_SQL_TOOLS_ENABLED is not true)")
        return False

    missing = _config_problem()
    if missing:
        log("SQL data tools NOT registered - missing settings", missing=missing)
        return False

    def _link_for(upc: Any) -> str:
        """NOV Explainer link for a UPC, or "" when links are off or the UPC doesn't fit."""
        if nov_link is None or upc in (None, ""):
            return ""
        try:
            return nov_link(str(upc)) or ""
        except Exception:
            return ""

    def _add_link_column(columns: List[str], rows: List[List[Any]]) -> Tuple[List[str], List[List[Any]]]:
        """Add a nov_explainer column built from the result's UPC column, if it has one."""
        lowered = [str(c).strip().lower() for c in columns]
        idx = next((lowered.index(n) for n in ("upc", "account", "parid") if n in lowered), None)
        if nov_link is None or idx is None:
            return columns, rows
        return columns + ["nov_explainer"], [list(r) + [_link_for(r[idx])] for r in rows]

    async def _call(
        tool: str,
        proc: str,
        params: List[Tuple[str, Any]],
        title: str,
        note: str,
        empty: str = "",
        link_rows: bool = False,
        link_upc: str = "",
    ) -> str:
        try:
            columns, rows = await asyncio.to_thread(_run_proc_with_retry, proc, params)
        except ImportError:
            log("SQL data tool failed - pyodbc not installed", tool=tool)
            raise ToolError(f"{_ERROR_PREFIX} the server is missing the pyodbc package.")
        except Exception as exc:
            log("SQL data tool failed", tool=tool, proc=proc, error=str(exc)[:500])
            raise ToolError(f"{_ERROR_PREFIX} {_clean_sql_error(exc)}")
        log("SQL data tool ok", tool=tool, proc=proc, rows=len(rows))
        if link_rows and rows:
            columns, rows = _add_link_column(columns, rows)
        text = _format_rows(title, columns, rows, note, empty)
        link = _link_for(link_upc) if (link_upc and rows) else ""
        if link:
            text += f"\nNOV Explainer (county sign-in required): {link}"
        return text

    def _bad_input(exc: Exception) -> str:
        # Raises so MCP clients get isError: true; the tools' "return _bad_input(...)"
        # lines never actually return.
        raise ToolError(f"{_ERROR_PREFIX} {exc}")

    async def ACES_Office_Volume(startDate: str, endDate: str, grain: str = "month") -> str:
        """
        Office call and walk-in visit counts for a date range, grouped by day,
        week (Monday start) or month. Open days come from the office calendar
        (weekends, holidays and closures are not open). Each row has
        first_day/last_day (periods are clipped to the requested range),
        calendar_days, open_days, closed_days, holidays (names of holidays in
        the period), closed_day_calls (calls that arrived while the office was
        closed), call totals (inbound, connected, abandoned, voicemail,
        overflow, no_outcome), abandon rate, visits, and per-open-day averages
        that use only open days. For daily averages use the avg_ columns, never
        calendar_days. Call outcomes add up: inbound = connected + abandoned +
        voicemail + no_outcome_calls. Whenever you list call outcomes, include
        no_outcome_calls (as "no recorded outcome") so the outcomes visibly add
        up to inbound. no_outcome_calls are inbound calls with no recorded
        outcome in the phone data; the cause isn't confirmed, so don't guess
        one. Overflow calls are already counted inside those outcomes, not on
        top of them.
        abandoned_pct on a closed day reflects after-hours calls, not staffed
        service, so don't present it as a service problem.
        open_days_missing_visits or days_missing_from_calendar above 0 means the
        data for those days is incomplete; say so. partial_days above 0 means the
        range includes today, whose counts are still coming in: totals include
        today, but the averages leave it out. On the row that includes today,
        latest_call_today is the time of the newest call in the data and
        last_loaded_mt is when call data last loaded (both Mountain time). Call
        data loads periodically during the business day, not live, so give
        today's counts "as of" latest_call_today (for example "8 calls as of
        8:53 AM"), never just "so far". abandoned_pct is blank for a period that
        is only today, because a partial day's rate isn't meaningful; don't
        work one out. If today has no row, today's calls haven't loaded yet;
        that doesn't mean there were none. Dates are YYYY-MM-DD; range up to
        about three years.
        """
        try:
            start, end, g = _parse_date(startDate, "startDate"), _parse_date(endDate, "endDate"), _parse_grain(grain)
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_Office_Volume", "ai_api.office_volume",
            [("start_date", start), ("end_date", end), ("grain", g)],
            f"Office volume, {start} to {end}, by {g}", _VOLUME_NOTE,
            empty=(
                f"No call or visit data loaded between {start} and {end}. If that includes "
                "today, today's calls may not have loaded yet; that doesn't mean there were none."
            ),
        )

    async def ACES_Forecast(startDate: str, endDate: str) -> str:
        """
        Current daily forecast of calls and walk-in visits (with 80% ranges)
        and suggested phone staffing for a date range of up to 120 days.
        Forecasts are planning estimates, not staff assignments.
        """
        try:
            start, end = _parse_date(startDate, "startDate"), _parse_date(endDate, "endDate")
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_Forecast", "ai_api.forecast",
            [("start_date", start), ("end_date", end)],
            f"Forecast, {start} to {end}",
            _VOLUME_NOTE + " Forecasts are estimates to support manager decisions, not staff assignments.",
        )

    async def ACES_Forecast_Accuracy(startDate: str, endDate: str) -> str:
        """
        How accurate past forecasts were for a date range: average miss (MAE),
        bias, share of days inside the forecast range, and the simple benchmark
        for comparison, for both calls and visits.
        """
        try:
            start, end = _parse_date(startDate, "startDate"), _parse_date(endDate, "endDate")
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_Forecast_Accuracy", "ai_api.forecast_accuracy",
            [("start_date", start), ("end_date", end)],
            f"Forecast accuracy, {start} to {end} (latest forecast issued before each day)",
            _VOLUME_NOTE,
        )

    async def ACES_NOV_Parcel(upc: str, taxYear: int = 0) -> str:
        """
        Notice of Value details for one account by UPC (the 18-digit number on
        the notice): situs address, class, notice land, buildings, full value on
        the notice, value history, 3% limit amount, taxable value, the reason the
        value changed, mailing date, and transfer/permit years. taxYear 0 returns
        all loaded years. Does not include exemptions. Also returns a link to the
        staff NOV Explainer page for the account. For an address, find the UPC
        with ACES_NOV_Search first. Percent-change fields are fractions (0.03
        means 3 percent); state them as percents. Explain a code (land use,
        reason) only if this result includes its meaning; otherwise give the
        code as is. Say to verify values in iasWorld before official use.
        """
        try:
            clean_upc, year = _parse_upc(upc), _parse_tax_year(taxYear)
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_NOV_Parcel", "ai_api.nov_parcel",
            [("upc", clean_upc), ("tax_year", year)],
            f"Notice of Value, UPC {clean_upc}", _DATA_NOTE,
            link_upc=clean_upc,
            empty=(
                f"No notice data found for UPC {clean_upc}"
                + (f" in tax year {year}" if year else " in any loaded tax year")
                + ". Check the number against the notice; the account may also not be in the latest ACES load."
            ),
        )

    async def ACES_NOV_Search(address: str, taxYear: int = 0) -> str:
        """
        Find accounts by the start of the situs (street) address, for example
        "2 Lauren Taylor Ct". City and ZIP are ignored. Returns up to 10 matches
        with UPC, roll type, class and a NOV Explainer link for each. Does not
        take a UPC; for a UPC use ACES_NOV_Parcel directly.
        """
        raw = str(address or "").strip()
        if _looks_like_upc(raw):
            return _bad_input(ValueError(
                "That looks like a UPC, not an address. Use ACES_NOV_Parcel for a UPC; "
                "NOV Search matches the start of a street address."
            ))
        normalized = ""
        if normalize_address is not None:
            try:
                normalized = normalize_address(raw) or ""
            except Exception:
                normalized = ""
        search = (normalized or raw).upper()[:100]
        try:
            year = _parse_tax_year(taxYear)
        except ValueError as exc:
            return _bad_input(exc)
        if len(search) < 4:
            return _bad_input(ValueError("address must be at least 4 characters."))
        return await _call(
            "ACES_NOV_Search", "ai_api.nov_search",
            [("address", search), ("tax_year", year)],
            f"Accounts matching '{search}'", _DATA_NOTE,
            link_rows=True,
            empty=(
                f"No accounts have a situs address starting with '{search}'. "
                "Check the house number and street spelling, and use the street "
                "type abbreviation (CT, DR, RD, ST, AVE)."
            ),
        )

    async def ACES_NOV_Coverage(taxYear: int = 0) -> str:
        """
        How many accounts rose more than 3% and what share of them have an
        explained, needs-check, or unexplained reason, by roll type.
        """
        try:
            year = _parse_tax_year(taxYear)
        except ValueError as exc:
            return _bad_input(exc)
        return await _call(
            "ACES_NOV_Coverage", "ai_api.nov_coverage",
            [("tax_year", year)],
            "Notice of Value explanation coverage", _DATA_NOTE,
        )

    async def _deed_freshness_note(tool: str) -> str:
        """One line on how current the deed data is; "" if the check itself isn't available."""
        try:
            columns, rows = await asyncio.to_thread(_run_proc_with_retry, "ai_api.deed_freshness", [])
        except Exception as exc:
            log("deed freshness check skipped", tool=tool, error=str(exc)[:300])
            return ""
        if not rows:
            return ""
        rec = dict(zip([str(c).lower() for c in columns], rows[0]))
        last = str(rec.get("last_deed_report_date") or "")[:10]
        try:
            missing = int(rec.get("open_days_missing") or 0)
        except (TypeError, ValueError):
            missing = 0
        if not last:
            return "DEED DATA GAP: no deed reports are loaded at all."
        if missing >= 2:
            return (
                f"DEED DATA GAP: the newest deed report loaded is for {last}, and {missing} open office days "
                "since then have no deed report. Recent deed figures are incomplete because of a loading gap, "
                "not zero activity; say so."
            )
        return f"Deed data is current through {last}."

    async def ACES_Deed_Volume(startDate: str, endDate: str, grain: str = "month") -> str:
        """
        Deed workload for a date range grouped by day, week or month: report
        days, reports, included entries, documents and unreviewed rows. These
        are volume figures, not a backlog: there is no certified backlog
        figure, so never describe them as one or estimate one from them.
        The result ends with how current the deed data is. If it says DEED DATA
        GAP, lead with that: missing days mean the reports weren't loaded, not
        that there was no activity.
        """
        try:
            start, end, g = _parse_date(startDate, "startDate"), _parse_date(endDate, "endDate"), _parse_grain(grain)
        except ValueError as exc:
            return _bad_input(exc)
        result = await _call(
            "ACES_Deed_Volume", "ai_api.deed_volume",
            [("start_date", start), ("end_date", end), ("grain", g)],
            f"Deed volume, {start} to {end}, by {g}", _VOLUME_NOTE,
        )
        note = await _deed_freshness_note("ACES_Deed_Volume")
        return f"{result}\n{note}" if note else result

    async def ACES_Assessment_Calendar(daysAhead: int = 90, daysBack: int = 30) -> str:
        """
        Assessment-calendar events around today (Notice of Value mailing,
        protest and reporting deadlines, tax bills and similar) with their
        expected effect on calls and visits. Each row has event_date, end_date
        (for multi-day events), event_name, category, phase, days_from_today
        (negative means it already happened), status (past, today, in
        progress, upcoming), expected_contact_impact, impact_start_date and
        impact_end_date (when contact volume is expected to be affected),
        in_impact_window (1 if today falls in that window), likely_contact_reason
        and classification_status. Older events still in their impact window
        are included, because they can explain current volume. daysAhead
        (1-400, default 90) and daysBack (0-365, default 30) set the window.
        expected_contact_impact is low, moderate, high or critical. If
        classification_status is UNCLASSIFIED_REVIEW, the event's impact
        hasn't been classified yet; say so instead of guessing one. Present
        impact and contact reasons as expected, not certain. Only give dates
        this tool returns; never work out a deadline that isn't listed. The
        calendar only covers the dates loaded into ACES; if an upcoming period
        returns nothing, say the calendar isn't loaded that far, not that
        nothing is happening.
        """
        try:
            ahead = _parse_int(daysAhead, "daysAhead", 1, 400, 90)
            back = _parse_int(daysBack, "daysBack", 0, 365, 30)
        except ValueError as exc:
            return _bad_input(exc)
        today = _today_mountain()
        return await _call(
            "ACES_Assessment_Calendar", "ai_api.assessment_calendar",
            [("as_of", today), ("days_ahead", ahead), ("days_back", back)],
            f"Assessment calendar around {today} ({back} days back, {ahead} ahead)", _CALENDAR_NOTE,
            empty=(
                f"No calendar dates between {today - timedelta(days=back)} and {today + timedelta(days=ahead)}. "
                "The ACES assessment calendar may not be loaded for this period."
            ),
        )

    async def _fetch(tool: str, proc: str, params: List[Tuple[str, Any]]) -> Tuple[List[str], List[List[Any]]]:
        """Run one procedure for the briefing; raises ToolError with a readable message."""
        try:
            columns, rows = await asyncio.to_thread(_run_proc_with_retry, proc, params)
        except ImportError:
            log("SQL data tool failed - pyodbc not installed", tool=tool, proc=proc)
            raise ToolError("the server is missing the pyodbc package.")
        except Exception as exc:
            log("SQL data tool failed", tool=tool, proc=proc, error=str(exc)[:500])
            raise ToolError(_clean_sql_error(exc))
        log("SQL data tool ok", tool=tool, proc=proc, rows=len(rows))
        return columns, rows

    def _section(title: str, result: Any, empty: str) -> str:
        if isinstance(result, BaseException):
            reason = str(result).strip().rstrip(".") or "the call failed"
            return f"{title}\nUNAVAILABLE: {reason}. Do not estimate this section.\n"
        columns, rows = result
        return _format_rows(title, columns, rows, "", empty).rstrip() + "\n"

    def _last_open_day(columns: List[str], rows: List[List[Any]]) -> Tuple[Optional[date], Optional[List[Any]]]:
        """Most recent row from a by-day ai_api.office_volume result with open_days > 0."""
        lowered = [str(c).strip().lower() for c in columns]
        if "first_day" not in lowered or "open_days" not in lowered:
            raise ToolError("office_volume did not return first_day and open_days columns")
        i_day, i_open = lowered.index("first_day"), lowered.index("open_days")
        best: Optional[date] = None
        best_row: Optional[List[Any]] = None
        for row in rows:
            try:
                day = date.fromisoformat(str(row[i_day])[:10])
                is_open = float(row[i_open] or 0) > 0
            except (TypeError, ValueError):
                continue
            if is_open and (best is None or day > best):
                best, best_row = day, row
        return best, best_row

    async def ACES_Daily_Briefing(asOf: str = "") -> str:
        """
        Morning briefing in one call: today's call and visit forecast; the last
        open day's actual volume and how it compared with its forecast; deed
        volume for the week ending that day; and assessment-calendar dates from
        14 days back to 45 days ahead. "Last open day" skips weekends, holidays
        and closures, so on a Monday it is the previous Friday. asOf
        (YYYY-MM-DD) runs the briefing as of an earlier date; leave it empty
        for today. Deed figures are volume, not backlog: there is no certified
        backlog figure, so never describe them as one. If the deed section has a
        DEED DATA GAP line, report it plainly. Call columns follow
        ACES_Office_Volume: inbound = connected + abandoned + voicemail +
        no_outcome_calls, and no_outcome_calls is listed with the other
        outcomes. A section marked
        UNAVAILABLE failed to load; say which one, and don't estimate it. For
        detail beyond the briefing, use the individual ACES_* tools.
        """
        tool = "ACES_Daily_Briefing"
        today = _today_mountain()
        if str(asOf or "").strip():
            try:
                today = _parse_date(asOf, "asOf")
            except ValueError as exc:
                return _bad_input(exc)

        # Step 1: find the last open day (and keep its volume row).
        try:
            vol_cols, vol_rows = await _fetch(
                tool, "ai_api.office_volume",
                [("start_date", today - timedelta(days=BRIEFING_LOOKBACK_DAYS)),
                 ("end_date", today - timedelta(days=1)), ("grain", "day")],
            )
            last_open, last_row = _last_open_day(vol_cols, vol_rows)
        except ToolError as exc:
            raise ToolError(f"{_ERROR_PREFIX} the briefing couldn't load recent office volume: {exc}")

        # Step 2: everything else, in parallel. A failed section is marked, not hidden.
        jobs = [
            _fetch(tool, "ai_api.forecast", [("start_date", today), ("end_date", today)]),
            _fetch(tool, "ai_api.assessment_calendar",
                   [("as_of", today), ("days_ahead", 45), ("days_back", 14)]),
        ]
        if last_open is not None:
            jobs += [
                _fetch(tool, "ai_api.forecast_accuracy", [("start_date", last_open), ("end_date", last_open)]),
                _fetch(tool, "ai_api.deed_volume",
                       [("start_date", last_open - timedelta(days=6)), ("end_date", last_open), ("grain", "day")]),
            ]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        forecast, calendar = results[0], results[1]

        lines = [f"Daily briefing for {today:%A, %B} {today.day}, {today.year} (Mountain time)"]
        if last_open is None:
            lines.append(f"No open office day found in the {BRIEFING_LOOKBACK_DAYS} days before {today}.")
        else:
            lines.append(f"Last open day: {last_open:%A, %B} {last_open.day}, {last_open.year}")
        lines.append("")
        lines.append(_section("== Today's forecast ==", forecast,
                              "No forecast for today (the office may be closed, or no forecast has been issued)."))
        if last_open is not None:
            accuracy, deeds = results[2], results[3]
            lines.append(_section("== Last open day: actual calls and visits ==",
                                  (vol_cols, [last_row]), "No volume row for the last open day."))
            lines.append(_section("== Last open day: forecast vs actual ==", accuracy,
                                  "No forecast was issued for the last open day."))
            lines.append(_section(f"== Deed volume, {last_open - timedelta(days=6)} to {last_open} ==", deeds,
                                  "No deed reports loaded for this week."))
            deed_note = await _deed_freshness_note(tool)
            if deed_note:
                lines.append(deed_note + "\n")
        lines.append(_section("== Assessment calendar (14 days back, 45 ahead) ==", calendar,
                              "No calendar dates in this window."))
        lines.append(_VOLUME_NOTE + " Forecasts are estimates to support manager decisions, not staff assignments. "
                     "Deed figures are volume, not backlog.")
        lines.append(_CALENDAR_NOTE)
        return "\n".join(lines)

    tools: Dict[str, Callable[..., Any]] = {
        "ACES_Office_Volume": ACES_Office_Volume,
        "ACES_Forecast": ACES_Forecast,
        "ACES_Forecast_Accuracy": ACES_Forecast_Accuracy,
        "ACES_NOV_Parcel": ACES_NOV_Parcel,
        "ACES_NOV_Search": ACES_NOV_Search,
        "ACES_NOV_Coverage": ACES_NOV_Coverage,
        "ACES_Deed_Volume": ACES_Deed_Volume,
        "ACES_Assessment_Calendar": ACES_Assessment_Calendar,
        "ACES_Daily_Briefing": ACES_Daily_Briefing,
    }
    for fn in tools.values():
        mcp.tool(fn)

    @mcp.custom_route("/data-tool", methods=["POST"])
    async def data_tool_route(request):
        """
        REST wrapper for Power Automate.
        Body: {"tool": "ACES_Office_Volume", "args": {"startDate": "2026-01-01", "endDate": "2026-09-30"}}
        Returns the standard contract: status, answer, task_id, project_id.
        """
        auth_response = require_rest_auth(request)
        if auth_response:
            return auth_response
        try:
            body = await request.json()
            body = body if isinstance(body, dict) else {}
        except Exception:
            body = {}

        tool_name = str(body.get("tool") or "").strip()
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        fn = tools.get(tool_name)
        if fn is None:
            return JSONResponse({
                "status": "failed",
                "answer": "Unknown data tool. Use one of: " + ", ".join(TOOL_NAMES) + ".",
                "task_id": "",
                "project_id": "",
            })
        try:
            answer = await fn(**args)
        except TypeError as exc:
            return JSONResponse({
                "status": "failed",
                "answer": f"{_ERROR_PREFIX} wrong arguments for {tool_name}: {exc}",
                "task_id": "",
                "project_id": "",
            })
        except ToolError as exc:
            return JSONResponse({
                "status": "failed",
                "answer": str(exc),
                "task_id": "",
                "project_id": "",
            })
        except Exception as exc:
            log("data tool route failed", tool=tool_name, error=str(exc)[:500])
            return JSONResponse({
                "status": "failed",
                "answer": f"{_ERROR_PREFIX} the {tool_name} call failed unexpectedly.",
                "task_id": "",
                "project_id": "",
            })
        return JSONResponse({
            "status": "completed",
            "answer": answer,
            "task_id": "",
            "project_id": "",
        })

    REGISTERED = True
    log(
        "SQL data tools registered",
        server=SQL_SERVER,
        database=SQL_DATABASE,
        auth=SQL_AUTH,
        tools=len(tools),
        retry_attempts=SQL_RETRY_ATTEMPTS,
        nov_links=nov_link is not None,
    )
    return True
