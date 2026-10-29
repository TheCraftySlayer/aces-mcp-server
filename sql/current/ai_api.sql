/*
  ai_api as it is in ASRCustomerService on asbcdbbernt, exported 2026-10-29 17:17 UTC
  by sql/export_ai_api.py. Generated: don't edit by hand. Rerun the script after any
  ai_api change and commit the result.

  10 sql stored procedure; 1 table(s).
*/

/* ---- Permissions on the ai_api schema and its objects ---- */
GRANT EXECUTE ON SCHEMA::[ai_api] TO [ai_api_executor];
GRANT EXECUTE ON [ai_api].[assessment_calendar] TO [ai_api_executor];
GRANT EXECUTE ON [ai_api].[season_outlook] TO [ASR_CS_User];

/* ---- Role memberships of everyone who can run ai_api procedures ---- */
/* These show what else those logins can do in the database, not just in ai_api. */
ALTER ROLE [ai_api_executor] ADD MEMBER [ASR_CS_User];
ALTER ROLE [db_datareader] ADD MEMBER [ASR_CS_User];
ALTER ROLE [db_datawriter] ADD MEMBER [ASR_CS_User];

/* ---- Table ai_api.tool_log (columns only) ---- */
--   log_id  bigint  NOT NULL
--   called_utc  datetime2(0)  NOT NULL
--   proc_name  sysname  NOT NULL
--   params  nvarchar(400)  NULL
--   caller  nvarchar(128)  NOT NULL

/* ---- ai_api.assessment_calendar (sql stored procedure) ---- */
/* ---------------------------------------------------------------------------
   @as_of comes from the server in Mountain time. If it's NULL, the procedure
   works out Mountain "today" itself, because Azure SQL's clock is UTC.

   Returns events that overlap the window, plus any older event whose expected
   contact-impact window covers today (so a mailing from five weeks ago still
   shows up while it's driving calls).

   Logs each call to ai_api.tool_log (55_ASSESSMENT_CALENDAR_LOGGING.sql).
   --------------------------------------------------------------------------- */
CREATE   PROCEDURE ai_api.assessment_calendar
    @as_of      date = NULL,
    @days_ahead int  = 90,
    @days_back  int  = 30
AS
BEGIN
    SET NOCOUNT ON;

    IF @as_of IS NULL
        SET @as_of = CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Mountain Standard Time' AS date);

    IF @days_ahead IS NULL OR @days_ahead < 1 OR @days_ahead > 400
        THROW 50001, 'days_ahead must be between 1 and 400.', 1;
    IF @days_back IS NULL OR @days_back < 0 OR @days_back > 365
        THROW 50002, 'days_back must be between 0 and 365.', 1;

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT('as_of=', @as_of, ' days_ahead=', @days_ahead, ' days_back=', @days_back));

    DECLARE @from date = DATEADD(day, -@days_back, @as_of);
    DECLARE @to   date = DATEADD(day,  @days_ahead, @as_of);

    WITH latest AS (
        -- One row per event; if an event was loaded more than once, keep the newest load.
        SELECT e.*,
               ROW_NUMBER() OVER (PARTITION BY e.assessment_key ORDER BY e.loaded_at DESC) AS rn
        FROM dbo.fact_assessment_event e
    ),
    parsed AS (
        SELECT title, category_code, phase_code, expected_contact_impact, impact_rank,
               likely_contact_reason, classification_status, is_all_day,
               TRY_CONVERT(date, LEFT(LTRIM(start_date), 10))        AS d_start,
               TRY_CONVERT(date, LEFT(LTRIM(end_date), 10))          AS d_end_raw,
               TRY_CONVERT(date, LEFT(LTRIM(impact_start_date), 10)) AS i_start,
               TRY_CONVERT(date, LEFT(LTRIM(impact_end_date), 10))   AS i_end
        FROM latest
        WHERE rn = 1
    ),
    ev AS (
        SELECT *,
               CASE
                   WHEN d_end_raw IS NULL OR d_end_raw < d_start THEN d_start
                          ELSE d_end_raw   -- end dates are inclusive (Part 0 check B, Oct 14, 2026)
               END AS d_end,
               ISNULL(i_end, i_start) AS i_end_eff
        FROM parsed
        WHERE d_start IS NOT NULL
    )
    SELECT
        d_start                                            AS event_date,
        CASE WHEN d_end > d_start THEN d_end END           AS end_date,
        title                                              AS event_name,
        category_code                                      AS category,
        phase_code                                         AS phase,
        DATEDIFF(day, @as_of, d_start)                     AS days_from_today,
        CASE
            WHEN @as_of BETWEEN d_start AND d_end
                THEN CASE WHEN d_end = d_start THEN 'today' ELSE 'in progress' END
            WHEN d_start > @as_of THEN 'upcoming'
            ELSE 'past'
        END                                                AS status,
        expected_contact_impact,
        i_start                                            AS impact_start_date,
        i_end                                              AS impact_end_date,
        CASE WHEN i_start IS NOT NULL AND @as_of BETWEEN i_start AND i_end_eff
             THEN 1 ELSE 0 END                             AS in_impact_window,
        likely_contact_reason,
        classification_status
    FROM ev
    WHERE (d_start <= @to AND d_end >= @from)
       OR (i_start IS NOT NULL AND @as_of BETWEEN i_start AND i_end_eff)
    ORDER BY d_start, impact_rank, title;
END
GO

/* ---- ai_api.deed_freshness (sql stored procedure) ---- */
/* ---------------------------------------------------------------------
   ai_api.deed_freshness
   How current the deed data is: the newest deed report date loaded, and
   how many open office days since then have no deed report. The deed
   tool and the Daily Briefing call it to flag loading gaps, so a gap
   shows up the day it starts instead of looking like a quiet week.
   Added 2026-10-15. Run the whole file on ACES_Test.
   The server's managed identity already has EXECUTE on the ai_api
   schema, so no new grant is needed.
   --------------------------------------------------------------------- */
CREATE   PROCEDURE ai_api.deed_freshness
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @today date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Mountain Standard Time' AS date);

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), '');

    DECLARE @last date = (SELECT MAX(TRY_CONVERT(date, report_date)) FROM ai_read.deed_daily);

    WITH cal AS (
        SELECT TRY_CONVERT(date, [date]) AS d, MAX(office_closed) AS office_closed
        FROM dbo.raw_calendar
        GROUP BY TRY_CONVERT(date, [date])
    )
    SELECT @last AS last_deed_report_date,
           -- Open days after the last report, up to yesterday (today's report may not be in yet).
           (SELECT COUNT(*) FROM cal
            WHERE cal.d > @last AND cal.d < @today AND cal.office_closed = 0) AS open_days_missing;
END
GO

/* ---- ai_api.deed_volume (sql stored procedure) ---- */
CREATE   PROCEDURE ai_api.deed_volume
    @start_date date,
    @end_date   date,
    @grain      varchar(5) = 'month'
AS
BEGIN
    SET NOCOUNT ON;

    IF @start_date IS NULL OR @end_date IS NULL OR @end_date < @start_date
        THROW 50001, 'start_date and end_date are required, and end_date must be on or after start_date.', 1;
    IF @grain NOT IN ('day', 'week', 'month')
        THROW 50001, 'grain must be day, week, or month.', 1;
    IF @grain = 'day' AND DATEDIFF(day, @start_date, @end_date) > 180
        THROW 50001, 'By day, the range is limited to about six months. Use week or month for longer ranges.', 1;
    IF @grain = 'week' AND DATEDIFF(day, @start_date, @end_date) > 1100
        THROW 50001, 'By week, the range is limited to about three years. Use month for longer ranges.', 1;
    IF @grain = 'month' AND DATEDIFF(month, @start_date, @end_date) > 179
        THROW 50001, 'By month, the range is limited to 15 years.', 1;

    -- Today in Mountain time. Azure SQL runs on UTC; this zone name handles daylight saving.
    DECLARE @today date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Mountain Standard Time' AS date);

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT('start=', @start_date, ' end=', @end_date, ' grain=', @grain));

    WITH d10 AS (
        SELECT v FROM (VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) t(v)
    ),
    n AS (   -- 0 to 9999, enough for 15 years of days
        SELECT a.v + 10 * b.v + 100 * c.v + 1000 * e.v AS i
        FROM d10 a CROSS JOIN d10 b CROSS JOIN d10 c CROSS JOIN d10 e
    ),
    dates AS (
        SELECT DATEADD(day, i, @start_date) AS d
        FROM n
        WHERE i <= DATEDIFF(day, @start_date, @end_date)
    ),
    cal AS (
        SELECT TRY_CONVERT(date, [date]) AS d, MAX(office_closed) AS office_closed
        FROM dbo.raw_calendar
        GROUP BY TRY_CONVERT(date, [date])
    ),
    grid AS (
        SELECT dt.d,
               CASE @grain
                    WHEN 'day'  THEN dt.d
                    WHEN 'week' THEN DATEADD(day, -((DATEPART(weekday, dt.d) + @@DATEFIRST - 2) % 7), dt.d)
                    ELSE DATEFROMPARTS(YEAR(dt.d), MONTH(dt.d), 1)
               END AS period_key,
               c.office_closed,
               dd.report_date AS has_report,
               dd.distinct_documents,
               dd.included_entries,
               dd.unreviewed_rows,
               dd.source
        FROM dates dt
        LEFT JOIN cal c ON c.d = dt.d
        LEFT JOIN ai_read.deed_daily dd ON dd.report_date = dt.d
    )
    SELECT
        MIN(g.d) AS first_day,
        MAX(g.d) AS last_day,
        COUNT(g.has_report) AS report_days,
        SUM(g.distinct_documents) AS documents,
        CAST(SUM(g.distinct_documents) * 1.0 / NULLIF(COUNT(g.distinct_documents), 0) AS decimal(9, 1))
            AS avg_documents_per_report_day,
        CASE WHEN COUNT(g.office_closed) = COUNT(*)
             THEN SUM(CASE WHEN g.office_closed = 0 THEN 1 ELSE 0 END) END AS open_days,
        CASE WHEN COUNT(g.office_closed) = COUNT(*)
             THEN SUM(CASE WHEN g.office_closed = 0 AND g.has_report IS NULL AND g.d < @today THEN 1 ELSE 0 END)
        END AS open_days_without_report,
        SUM(CASE WHEN g.has_report IS NOT NULL AND g.distinct_documents IS NULL THEN 1 ELSE 0 END)
            AS days_needing_review,
        CASE WHEN COUNT(g.has_report) = 0 THEN NULL
             WHEN SUM(CASE WHEN g.source = 'BATCH' THEN 1 ELSE 0 END) = 0 THEN 'history snapshot'
             WHEN SUM(CASE WHEN g.source = 'BATCH' THEN 1 ELSE 0 END) = COUNT(g.has_report) THEN 'daily reports'
             ELSE 'history snapshot + daily reports'
        END AS data_source,
        SUM(CASE WHEN g.source = 'BATCH' THEN 1 ELSE 0 END) AS daily_report_days,
        SUM(CASE WHEN g.source = 'BATCH' THEN g.included_entries END) AS included_entries,
        SUM(CASE WHEN g.source = 'BATCH' THEN g.unreviewed_rows END) AS unreviewed_rows
    FROM grid g
    GROUP BY g.period_key
    ORDER BY g.period_key;
END
GO

/* ---- ai_api.forecast (sql stored procedure) ---- */
-- Current daily forecast for a date range (up to 120 days).
-- 48_FORECAST_95_RANGES.sql: 95% ranges added after review_status.
-- 51_FORECAST_95_RANGE_FIX.sql: each side of the 95% range is at least 1.529369 times that
--   side of the 80% range (the forecast job widens the 80% range by lead time, not the 95%).
-- 52_FORECAST_PHONES_NEEDED.sql: phones_needed_peak_hour (Erlang C, 80% within 60 s).
-- 53_TAX_SEASON_FORECAST.sql: season_ columns from dbo.aces_season_forecast, scaled by
--   dbo.aces_season_status (NULL on other dates).
-- 54_SEASON_BUILDER.sql: warm-up days hidden; season_name and season_basis added last.
CREATE PROCEDURE [ai_api].[forecast]
    @start_date date,
    @end_date   date
AS
BEGIN
    SET NOCOUNT ON;

    IF @start_date IS NULL OR @end_date IS NULL OR @end_date < @start_date
        THROW 50001, 'start_date and end_date are required, and end_date must be on or after start_date.', 1;
    IF DATEDIFF(day, @start_date, @end_date) > 120
        THROW 50001, 'The forecast range is limited to 120 days.', 1;

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT('start=', @start_date, ' end=', @end_date));

    -- 95% vs 80% width on a normal curve: 1.959964 / 1.281552
    DECLARE @k decimal(9,6) = 1.529369;

    WITH src AS (
        SELECT f.target_date, f.as_of_date,
               f.calls, f.calls_low80, f.calls_high80,
               f.visitors, f.visitors_low80, f.visitors_high80,
               f.phone_staff, f.peak_phone_staff, f.review_status,
               f.calls_low95, f.calls_high95, f.visitors_low95, f.visitors_high95,
               d.peak_phone_staff_service_level AS peak_phone_staff_service_level
        FROM ai_read.forecast_current_daily AS f
        LEFT JOIN dbo.aces_forecast_daily AS d
               ON d.run_id = f.run_id AND d.target_date = f.target_date
        WHERE f.target_date BETWEEN @start_date AND @end_date
    )
    SELECT s.target_date, s.as_of_date,
           s.calls, s.calls_low80, s.calls_high80,
           s.visitors, s.visitors_low80, s.visitors_high80,
           s.phone_staff, s.peak_phone_staff, s.review_status,
           calls_low95     = CASE WHEN w.cl < s.calls_low95     THEN w.cl ELSE s.calls_low95     END,
           calls_high95    = CASE WHEN w.ch > s.calls_high95    THEN w.ch ELSE s.calls_high95    END,
           visitors_low95  = CASE WHEN w.vl < s.visitors_low95  THEN w.vl ELSE s.visitors_low95  END,
           visitors_high95 = CASE WHEN w.vh > s.visitors_high95 THEN w.vh ELSE s.visitors_high95 END,
           phones_needed_peak_hour = s.peak_phone_staff_service_level,
           season_calls         = CAST(ROUND(sf.calls         * sa.adj_c, 0) AS int),
           season_calls_low     = CAST(ROUND(sf.calls_low     * sa.adj_c, 0) AS int),
           season_calls_high    = CAST(ROUND(sf.calls_high    * sa.adj_c, 0) AS int),
           season_visitors      = CAST(ROUND(sf.visitors      * sa.adj_v, 0) AS int),
           season_visitors_low  = CAST(ROUND(sf.visitors_low  * sa.adj_v, 0) AS int),
           season_visitors_high = CAST(ROUND(sf.visitors_high * sa.adj_v, 0) AS int),
           season_phones_needed_peak_hour = CASE WHEN sf.target_date IS NOT NULL THEN
               dbo.aces_fn_erlang_agents(sf.calls * sa.adj_c * sf.peak_hour_share, sf.handle_seconds, 0.80) END,
           season_phones_needed_peak_hour_high = CASE WHEN sf.target_date IS NOT NULL THEN
               dbo.aces_fn_erlang_agents(sf.calls_high * sa.adj_c * sf.peak_hour_share, sf.handle_seconds, 0.80) END,
           season_adjustment    = CASE WHEN sf.target_date IS NOT NULL THEN CAST(sa.adj_c AS decimal(5, 2)) END,
           season_days_checked  = CASE WHEN sf.target_date IS NOT NULL THEN sa.days_c END,
           season_name          = sn.season_name,
           season_basis         = sn.season_basis
    FROM src AS s
    CROSS APPLY (SELECT
        cl = CAST(ROUND(IIF(s.calls - @k * (s.calls - s.calls_low80) < 0, 0,
                            s.calls - @k * (s.calls - s.calls_low80)), 0) AS int),
        ch = CAST(ROUND(s.calls + @k * (s.calls_high80 - s.calls), 0) AS int),
        vl = CAST(ROUND(IIF(s.visitors - @k * (s.visitors - s.visitors_low80) < 0, 0,
                            s.visitors - @k * (s.visitors - s.visitors_low80)), 0) AS int),
        vh = CAST(ROUND(s.visitors + @k * (s.visitors_high80 - s.visitors), 0) AS int)
    ) AS w
    OUTER APPLY (SELECT TOP (1) x.season_key, x.target_date, x.calls, x.calls_low, x.calls_high,
                        x.visitors, x.visitors_low, x.visitors_high, x.peak_hour_share, x.handle_seconds
                 FROM dbo.aces_season_forecast AS x
                 WHERE x.target_date = s.target_date AND x.show_in_forecast = 1
                 ORDER BY x.season_key DESC) AS sf
    OUTER APPLY (SELECT ISNULL(MAX(st.adj_calls), 1.0)    AS adj_c,
                        ISNULL(MAX(st.adj_visitors), 1.0) AS adj_v,
                        ISNULL(MAX(st.days_calls), 0)     AS days_c
                 FROM dbo.aces_season_status AS st
                 WHERE st.season_key = sf.season_key) AS sa
    OUTER APPLY (SELECT MAX(z.season_name) AS season_name, MAX(z.season_basis) AS season_basis
                 FROM dbo.aces_season AS z
                 WHERE z.season_key = sf.season_key) AS sn
    ORDER BY s.target_date;
END
GO

/* ---- ai_api.forecast_accuracy (sql stored procedure) ---- */
/* Fix for ai_api.forecast_accuracy (Oct 14, 2026)
   The only change: score same-day forecasts (as_of_date <= target_date instead of <).
   Every row in ai_read.forecast_accuracy_daily has as_of_date = target_date, so the old
   strict < filtered out all of them and days_scored was always 0.

   RUN THIS ONLY IF a forecast run dated day D uses actuals through D-1 and nothing from D
   itself. Otherwise same-day scores would include data the forecast already saw. */

-- Forecast accuracy summary. Scores the latest forecast issued on or before each
-- target day, so a day isn't counted once per run. Same-day forecasts count because
-- each run uses actuals through the previous day only.
CREATE   PROCEDURE ai_api.forecast_accuracy
    @start_date date,
    @end_date   date
AS
BEGIN
    SET NOCOUNT ON;

    IF @start_date IS NULL OR @end_date IS NULL OR @end_date < @start_date
        THROW 50001, 'start_date and end_date are required, and end_date must be on or after start_date.', 1;
    IF DATEDIFF(day, @start_date, @end_date) > 1100
        THROW 50001, 'The date range is limited to about three years.', 1;

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT('start=', @start_date, ' end=', @end_date));

    WITH latest AS (
        SELECT a.*,
               ROW_NUMBER() OVER (PARTITION BY a.target_date ORDER BY a.as_of_date DESC, a.run_id DESC) AS rn
        FROM ai_read.forecast_accuracy_daily a
        WHERE a.target_date BETWEEN @start_date AND @end_date
          AND a.as_of_date <= a.target_date
          AND a.calls_actual IS NOT NULL
    )
    SELECT COUNT(*)          AS days_scored,
           MIN(target_date)  AS first_day,
           MAX(target_date)  AS last_day,
           CAST(AVG(ABS(CAST(calls_forecast  - calls_actual AS float))) AS decimal(9,1)) AS calls_mae,
           CAST(AVG(ABS(CAST(calls_benchmark - calls_actual AS float))) AS decimal(9,1)) AS calls_benchmark_mae,
           CAST(AVG(CAST(calls_forecast - calls_actual AS float))       AS decimal(9,1)) AS calls_bias,
           CAST(100.0 * AVG(CAST(calls_in_range AS float))              AS decimal(5,1)) AS calls_in_range_pct,
           CAST(AVG(ABS(CAST(visitors_forecast  - visitors_actual AS float))) AS decimal(9,1)) AS visitors_mae,
           CAST(AVG(ABS(CAST(visitors_benchmark - visitors_actual AS float))) AS decimal(9,1)) AS visitors_benchmark_mae,
           CAST(AVG(CAST(visitors_forecast - visitors_actual AS float))       AS decimal(9,1)) AS visitors_bias,
           CAST(100.0 * AVG(CAST(visitors_in_range AS float))                 AS decimal(5,1)) AS visitors_in_range_pct
    FROM latest
    WHERE rn = 1;
END
GO

/* ---- ai_api.nov_coverage (sql stored procedure) ---- */
-- NOV explanation coverage by roll type.
CREATE   PROCEDURE ai_api.nov_coverage
    @tax_year smallint = NULL
AS
BEGIN
    SET NOCOUNT ON;

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT('tax_year=', @tax_year));

    SELECT tax_year, roll_type, accounts_up_over_3, pct_explained, pct_needs_check, pct_unexplained
    FROM ai_read.nov_coverage
    WHERE @tax_year IS NULL OR tax_year = @tax_year
    ORDER BY tax_year DESC, roll_type;
END
GO

/* ---- ai_api.nov_parcel (sql stored procedure) ---- */
-- One account's notice-of-value card by UPC. Full value is the notice
-- total (notice land + buildings). No exemptions or net taxable value.
CREATE   PROCEDURE ai_api.nov_parcel
    @upc      varchar(30),
    @tax_year smallint = NULL
AS
BEGIN
    SET NOCOUNT ON;

    SET @upc = UPPER(LTRIM(RTRIM(@upc)));
    IF @upc IS NULL OR LEN(@upc) < 6 OR LEN(@upc) > 20
        THROW 50001, 'upc is required (6 to 20 characters).', 1;

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT('upc=', @upc, ' tax_year=', @tax_year));

    SELECT TOP (5)
           p.tax_year,
           p.roll_type,
           p.upc,
           p.situs_address,
           p.value_class,
           p.luc,
           COALESCE(p.land_curr_notice, p.land_curr)                          AS land_notice,
           p.bldg_curr                                                        AS buildings,
           COALESCE(p.land_curr_notice, p.land_curr) + ISNULL(p.bldg_curr, 0) AS full_value_notice,
           p.value_history,
           p.legal_limit,
           p.taxable_value,
           p.reason_code,
           r.label                                                            AS reason_label,
           p.mailed_date,
           p.nbhd_median_pct,
           p.deed_type,
           p.transfer_year,
           p.permit_year
    FROM dbo.aces_v_nov_current p
    LEFT JOIN dbo.aces_nov_reason r ON r.reason_code = p.reason_code
    WHERE p.upc = @upc
      AND (@tax_year IS NULL OR p.tax_year = @tax_year)
    ORDER BY p.tax_year DESC, p.roll_type;
END
GO

/* ---- ai_api.nov_search (sql stored procedure) ---- */
-- Find accounts by the start of the situs address (up to 10 matches).
CREATE   PROCEDURE ai_api.nov_search
    @address  nvarchar(100),
    @tax_year smallint = NULL
AS
BEGIN
    SET NOCOUNT ON;

    SET @address = UPPER(LTRIM(RTRIM(@address)));
    IF @address IS NULL OR LEN(@address) < 4
        THROW 50001, 'address must be at least 4 characters.', 1;

    -- Treat LIKE wildcards in the input as literal characters.
    DECLARE @pattern nvarchar(220) =
        REPLACE(REPLACE(REPLACE(@address, '[', '[[]'), '%', '[%]'), '_', '[_]') + N'%';

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT('address=', LEFT(@address, 100), ' tax_year=', @tax_year));

    SELECT TOP (10) tax_year, roll_type, upc, situs_address, value_class
    FROM dbo.aces_v_nov_current
    WHERE situs_address LIKE @pattern
      AND (@tax_year IS NULL OR tax_year = @tax_year)
    ORDER BY situs_address, tax_year DESC;
END
GO

/* ---- ai_api.office_volume (sql stored procedure) ---- */
/* ---------------------------------------------------------------------
   ai_api.office_volume
   - partial_days: how many days in the period are today (still loading).
   - Totals still include today; averages use only complete open days.
   - open_days_missing_visits ignores today, where missing visits are normal.
   - no_outcome_calls: inbound calls with no connected, abandoned or
     voicemail flag. Added 2026-10-15 so call totals always add up.
   - latest_call_today / last_loaded_mt: on the row that includes today,
     the newest call in the data and the last call-data load, both in
     Mountain time. Call data loads hourly during the business day, so
     today's counts are "as of" these times. Added 2026-10-15.
   - abandoned_pct is NULL for a period that is only today (a partial
     day's rate isn't meaningful) or has no open days (a handful of
     after-hours calls isn't an abandoned rate). Added 2026-10-15.
   - 2026-10-15 (v2):
     * Days come from the calendar plus the data, so an open day with no
       data still shows up. open_days_missing_calls counts those days.
       calendar_days and closed_days are now true calendar counts for
       dates raw_calendar covers (it starts Dec 3, 2020). Future dates
       are left out.
     * forecast_basis_calls: the call count the forecast is built and
       scored on (dbo.aces_v_forecast_inbound_call: KPI-eligible customer
       service calls). inbound_calls is everything that rang and runs
       roughly 30 to 60 percent higher. Compare forecasts to
       forecast_basis_calls only.
     * Averages divide by days that actually have data, so a missed load
       can't drag them down.
     * New columns are at the end so existing column positions don't move.
   Run the whole file on ACES_Test.
   --------------------------------------------------------------------- */

CREATE   PROCEDURE ai_api.office_volume
    @start_date date,
    @end_date   date,
    @grain      varchar(5) = 'month'
AS
BEGIN
    SET NOCOUNT ON;

    IF @start_date IS NULL OR @end_date IS NULL OR @end_date < @start_date
        THROW 50001, 'start_date and end_date are required, and end_date must be on or after start_date.', 1;
    IF DATEDIFF(day, @start_date, @end_date) > 1100
        THROW 50001, 'The date range is limited to about three years.', 1;
    IF @grain NOT IN ('day', 'week', 'month')
        THROW 50001, 'grain must be day, week, or month.', 1;

    -- Today in Mountain time. Azure SQL runs on UTC; this zone name handles daylight saving.
    DECLARE @today date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Mountain Standard Time' AS date);

    -- Text keys for tables that store dates as 'yyyy-mm-dd' text.
    DECLARE @start_key nvarchar(10) = CONVERT(nvarchar(10), @start_date, 23),
            @end_key   nvarchar(10) = CONVERT(nvarchar(10), @end_date, 23);

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT('start=', @start_date, ' end=', @end_date, ' grain=', @grain));

    -- Data freshness, only when the range includes today.
    DECLARE @latest_call_today nvarchar(20) = NULL,
            @last_loaded_mt    nvarchar(30) = NULL;
    IF @start_date <= @today AND @end_date >= @today
    BEGIN
        DECLARE @today_key     nvarchar(10) = CONVERT(nvarchar(10), @today, 23),
                @yesterday_key nvarchar(10) = CONVERT(nvarchar(10), DATEADD(day, -1, @today), 23),
                @latest_ts     datetime2(0),
                @loaded        datetimeoffset(7);

        -- event_ts_local is local time text like '2026-10-15 08:53:38'.
        SELECT @latest_ts = MAX(TRY_CONVERT(datetime2(0), LEFT(event_ts_local, 19)))
        FROM dbo.fact_call_session
        WHERE event_date = @today_key AND kpi_eligible = 1 AND direction = 'INBOUND';

        -- loaded_at is UTC text like '2026-10-15T15:12:01.020029+00:00'.
        SELECT @loaded = MAX(TRY_CONVERT(datetimeoffset(7), loaded_at))
        FROM dbo.fact_call_session
        WHERE event_date >= @yesterday_key;

        SET @latest_call_today = FORMAT(@latest_ts, 'h:mm tt', 'en-US');
        SET @last_loaded_mt    = FORMAT(@loaded AT TIME ZONE 'Mountain Standard Time', 'yyyy-MM-dd h:mm tt', 'en-US');
    END;

    WITH c AS (
        SELECT TRY_CONVERT(date, [date]) AS d,
               inbound_calls, connected_calls, abandoned_calls, voicemail_calls, overflow_calls
        FROM ai_read.call_daily
        WHERE TRY_CONVERT(date, [date]) BETWEEN @start_date AND @end_date
    ), v AS (
        SELECT TRY_CONVERT(date, [date]) AS d, visits
        FROM ai_read.visitor_daily
        WHERE TRY_CONVERT(date, [date]) BETWEEN @start_date AND @end_date
    ), fb AS (
        -- The forecast's own call count, from the same view the forecast is scored on.
        SELECT TRY_CONVERT(date, event_date, 23) AS d, COUNT(*) AS forecast_basis_calls
        FROM dbo.aces_v_forecast_inbound_call
        WHERE event_date BETWEEN @start_key AND @end_key
        GROUP BY TRY_CONVERT(date, event_date, 23)
    ), cal AS (
        -- One row per date, in case the raw table has duplicates.
        SELECT TRY_CONVERT(date, [date]) AS d,
               MAX(office_closed) AS office_closed,
               MAX(holiday)       AS holiday,
               MAX(notes)         AS notes
        FROM dbo.raw_calendar
        WHERE TRY_CONVERT(date, [date]) BETWEEN @start_date AND @end_date
        GROUP BY TRY_CONVERT(date, [date])
    ), data_days AS (
        SELECT d FROM c
        UNION SELECT d FROM v
        UNION SELECT d FROM fb
    ), all_days AS (
        -- Every calendar day in the range plus any data day the calendar lacks
        -- (raw_calendar starts Dec 3, 2020), so a day with no data still shows up.
        SELECT COALESCE(cal.d, dd.d) AS d,
               cal.d AS cal_d,
               cal.office_closed,
               cal.holiday,
               cal.notes
        FROM cal
        FULL OUTER JOIN data_days AS dd ON dd.d = cal.d
    ), days AS (
        SELECT a.d,
               a.cal_d,
               a.holiday,
               a.notes,
               c.inbound_calls, c.connected_calls, c.abandoned_calls,
               c.voicemail_calls, c.overflow_calls,
               v.visits,
               fb.forecast_basis_calls,
               CAST(CASE
                        WHEN a.office_closed IS NOT NULL THEN 1 - a.office_closed
                        WHEN ISNULL(c.connected_calls, 0) > 0 OR ISNULL(v.visits, 0) > 0 THEN 1
                        ELSE 0
                    END AS int) AS is_open,
               -- Today's counts keep growing during the day, so today is not a complete day yet.
               CAST(CASE WHEN a.d >= @today THEN 1 ELSE 0 END AS int) AS is_partial
        FROM all_days AS a
        LEFT JOIN c  ON c.d  = a.d
        LEFT JOIN v  ON v.d  = a.d
        LEFT JOIN fb ON fb.d = a.d
        WHERE a.d BETWEEN @start_date AND @end_date
          AND a.d <= @today   -- future calendar days aren't missing data
    ), p AS (
        SELECT CASE @grain
                   WHEN 'day'  THEN d
                   WHEN 'week' THEN DATEADD(day, -(DATEDIFF(day, '19000101', d) % 7), d)
                   ELSE DATEFROMPARTS(YEAR(d), MONTH(d), 1)
               END AS period_start,
               days.*
        FROM days
    ), g AS (
        SELECT period_start,
               MIN(d)   AS first_day,
               MAX(d)   AS last_day,
               COUNT(*) AS calendar_days,
               SUM(is_open) AS open_days,
               -- Missing data only counts on complete days; today often hasn't fully loaded.
               SUM(CASE WHEN is_open = 1 AND is_partial = 0 AND inbound_calls IS NULL THEN 1 ELSE 0 END) AS open_days_missing_calls,
               SUM(CASE WHEN is_open = 1 AND is_partial = 0 AND visits IS NULL THEN 1 ELSE 0 END) AS open_days_missing_visits,
               SUM(CASE WHEN cal_d IS NULL THEN 1 ELSE 0 END) AS days_missing_from_calendar,
               SUM(is_partial) AS partial_days,
               SUM(inbound_calls)   AS inbound_calls,
               SUM(connected_calls) AS connected_calls,
               SUM(abandoned_calls) AS abandoned_calls,
               SUM(voicemail_calls) AS voicemail_calls,
               SUM(overflow_calls)  AS overflow_calls,
               -- Inbound calls with none of the three outcome flags. The flags don't overlap,
               -- so subtraction is exact.
               SUM(ISNULL(inbound_calls, 0) - ISNULL(connected_calls, 0)
                   - ISNULL(abandoned_calls, 0) - ISNULL(voicemail_calls, 0)) AS no_outcome_calls,
               SUM(CASE WHEN is_open = 0 THEN ISNULL(inbound_calls, 0) ELSE 0 END) AS closed_day_calls,
               SUM(visits) AS visits_raw,
               COUNT(visits) AS visit_rows,
               SUM(forecast_basis_calls) AS forecast_basis_calls,
               -- Complete open days, totals and how many of those days have data.
               SUM(CASE WHEN is_open = 1 AND is_partial = 0 THEN inbound_calls END)           AS complete_open_inbound_calls,
               COUNT(CASE WHEN is_open = 1 AND is_partial = 0 THEN inbound_calls END)         AS complete_open_call_days,
               SUM(CASE WHEN is_open = 1 AND is_partial = 0 THEN visits END)                  AS complete_open_visits,
               COUNT(CASE WHEN is_open = 1 AND is_partial = 0 THEN visits END)                AS complete_open_visit_days,
               SUM(CASE WHEN is_open = 1 AND is_partial = 0 THEN forecast_basis_calls END)    AS complete_open_fb_calls,
               COUNT(CASE WHEN is_open = 1 AND is_partial = 0 THEN forecast_basis_calls END)  AS complete_open_fb_days,
               -- Holidays in the period, e.g. "10/12 Indigenous Peoples' Day". Pipes are swapped
               -- out because the MCP tool uses " | " to separate columns.
               STRING_AGG(CASE WHEN holiday = 1
                               THEN CONCAT(LEFT(CONVERT(varchar(10), d, 101), 5), ' ',
                                           REPLACE(COALESCE(notes, N'holiday'), N'|', N'/'))
                          END, N'; ') WITHIN GROUP (ORDER BY d) AS holidays
        FROM p
        GROUP BY period_start
    )
    SELECT period_start,
           first_day,
           last_day,
           calendar_days,
           open_days,
           calendar_days - open_days AS closed_days,
           holidays,
           closed_day_calls,
           open_days_missing_visits,
           days_missing_from_calendar,
           partial_days,
           inbound_calls,
           connected_calls,
           abandoned_calls,
           voicemail_calls,
           overflow_calls,
           -- Blank when the period is only today or has no open days.
           CASE WHEN partial_days = calendar_days OR open_days = 0 THEN NULL
                ELSE CAST(100.0 * abandoned_calls / NULLIF(inbound_calls, 0) AS decimal(5,1))
           END AS abandoned_pct,
           CASE WHEN visit_rows = 0 AND open_days = 0 THEN 0 ELSE visits_raw END AS visits,
           -- Averages use complete open days that have data, so a half-loaded today
           -- or a missed load can't drag them down.
           CAST(1.0 * complete_open_inbound_calls / NULLIF(complete_open_call_days, 0) AS decimal(7,1)) AS avg_inbound_calls_per_open_day,
           CAST(1.0 * complete_open_visits / NULLIF(complete_open_visit_days, 0) AS decimal(7,1)) AS avg_visits_per_open_day,
           no_outcome_calls,
           CASE WHEN partial_days > 0 THEN @latest_call_today END AS latest_call_today,
           CASE WHEN partial_days > 0 THEN @last_loaded_mt    END AS last_loaded_mt,
           -- v2 columns, kept at the end so existing positions don't move.
           open_days_missing_calls,
           forecast_basis_calls,
           CAST(1.0 * complete_open_fb_calls / NULLIF(complete_open_fb_days, 0) AS decimal(7,1)) AS avg_forecast_basis_calls_per_open_day
    FROM g
    ORDER BY period_start;
END
GO

/* ---- ai_api.season_outlook (sql stored procedure) ---- */
/* ---------------------------------------------------------------- 5. ai_api.season_outlook */
CREATE   PROCEDURE ai_api.season_outlook
    @start_date date,
    @end_date   date
AS
BEGIN
    SET NOCOUNT ON;

    IF @start_date IS NULL OR @end_date IS NULL OR @end_date < @start_date
        THROW 50001, 'start_date and end_date are required, and end_date must be on or after start_date.', 1;
    IF DATEDIFF(day, @start_date, @end_date) > 200
        THROW 50001, 'The season outlook range is limited to 200 days.', 1;

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT('start=', @start_date, ' end=', @end_date));

    SELECT f.target_date,
           s.season_name, s.season_basis, s.mailing_date, s.landing_date, s.deadline_date,
           season_calls         = CAST(ROUND(f.calls         * a.adj_c, 0) AS int),
           season_calls_low     = CAST(ROUND(f.calls_low     * a.adj_c, 0) AS int),
           season_calls_high    = CAST(ROUND(f.calls_high    * a.adj_c, 0) AS int),
           season_visitors      = CAST(ROUND(f.visitors      * a.adj_v, 0) AS int),
           season_visitors_low  = CAST(ROUND(f.visitors_low  * a.adj_v, 0) AS int),
           season_visitors_high = CAST(ROUND(f.visitors_high * a.adj_v, 0) AS int),
           season_phones_needed_peak_hour      = dbo.aces_fn_erlang_agents(f.calls * a.adj_c * f.peak_hour_share, f.handle_seconds, 0.80),
           season_phones_needed_peak_hour_high = dbo.aces_fn_erlang_agents(f.calls_high * a.adj_c * f.peak_hour_share, f.handle_seconds, 0.80),
           season_adjustment    = CAST(a.adj_c AS decimal(5, 2)),
           season_days_checked  = a.days_c,
           level_basis = CASE WHEN a.days_c = 0 THEN 'planning guess: normal level estimated from the October before'
                              ELSE CONCAT('adjusted to ', a.days_c, ' finished days before and during the season') END
      FROM dbo.aces_season_forecast AS f
      JOIN dbo.aces_season AS s ON s.season_key = f.season_key
     OUTER APPLY (SELECT ISNULL(MAX(st.adj_calls), 1.0)    AS adj_c,
                         ISNULL(MAX(st.adj_visitors), 1.0) AS adj_v,
                         ISNULL(MAX(st.days_calls), 0)     AS days_c
                    FROM dbo.aces_season_status AS st
                   WHERE st.season_key = f.season_key) AS a
     WHERE f.show_in_forecast = 1
       AND f.target_date BETWEEN @start_date AND @end_date
     ORDER BY f.target_date, f.season_key;
END;
GO
