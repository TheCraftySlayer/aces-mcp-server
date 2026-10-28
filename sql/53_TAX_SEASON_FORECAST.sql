/*
  53_TAX_SEASON_FORECAST.sql

  Tax-bill season forecast for Nov 2 to Dec 23, 2026, in ASRCustomerService, so A.C.E.S.
  shows it next to the regular forecast.

  Why: the regular forecast has no past tax-bill seasons in its calendar, so it expects a normal
  November. In 2023, 2024 and 2025, calls ran about 2.5 to 3 times October for two weeks after
  the bills went out and stayed high into December; walk-ins about doubled for five weeks.

  How the numbers were built (Oct 28, 2026):
    - Each past season lined up by mailing day (first business day on or after Nov 1:
      2023-11-01, 2024-11-01, 2025-11-03; 2026-11-02) and turned into a day-by-day multiplier of
      that year's October (Oct 1 to 24), weekday adjusted, averaged over the three years.
    - Applied to October 2026's normal level: 76.3 customer-facing calls and 39.8 walk-ins a day.
    - Ranges (80%) from how far each year landed when predicted from the other two:
      calls x0.72 to x1.42, walk-ins x0.70 to x1.36.
    - Tested: 2025 predicted from 2023 and 2024 only missed by 29 calls a day, against 80 for a
      normal-month plan and 42 for the regular forecast running one day ahead.

  What this script creates (run as ASR_CS_Owner; safe to rerun; a rerun resets the season rows):
    1. dbo.aces_season_forecast: the 35 season days, with actuals filled in as the season runs.
    2. dbo.aces_season_status: how this season is running (the adjustment).
    3. dbo.aces_fn_erlang_agents: Erlang C people on the phones for an hour (same method as
       ServiceLevelStaffing.cs in the ACES source).
    4. dbo.aces_season_refresh: run it once a day after the loads. It counts actual
       customer-facing calls and walk-ins for finished season days and scales the rest of the
       season to match. It uses days from the 4th season day on (Nov 5), skips days that look
       partly loaded (under 40% of the season number), and moves gradually
       (adjustment = 1 + (actual / season - 1) x days / (days + 2), kept between 0.6 and 1.6).
    5. ai_api.forecast: same as 52, plus season_ columns for season dates (NULL on other dates).

  To undo the procedure change, rerun 52_FORECAST_PHONES_NEEDED.sql. The tables, function and
  refresh procedure can stay; nothing else reads them.

  If any part prints an error, stop and send the messages.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

/* ---------------------------------------------------------------- 1. tables */
IF OBJECT_ID(N'dbo.aces_season_forecast', N'U') IS NULL
    CREATE TABLE dbo.aces_season_forecast (
        season_key            nvarchar(40)  NOT NULL,
        target_date           date          NOT NULL,
        season_day            int           NOT NULL,   -- open days since the mailing day (0)
        calls                 float         NOT NULL,   -- customer-facing calls, before the adjustment
        calls_low             float         NOT NULL,
        calls_high            float         NOT NULL,
        visitors              float         NOT NULL,
        visitors_low          float         NOT NULL,
        visitors_high         float         NOT NULL,
        peak_hour_share       float         NOT NULL,   -- share of the day's calls in its busiest hour
        handle_seconds        float         NOT NULL,
        built_from            nvarchar(200) NOT NULL,
        actual_calls          int           NULL,       -- filled by dbo.aces_season_refresh
        actual_visitors       int           NULL,
        actuals_refreshed_utc datetime2(0)  NULL,
        loaded_utc            datetime2(0)  NOT NULL CONSTRAINT DF_aces_season_forecast_loaded DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_aces_season_forecast PRIMARY KEY (season_key, target_date)
    );

IF OBJECT_ID(N'dbo.aces_season_status', N'U') IS NULL
    CREATE TABLE dbo.aces_season_status (
        season_key     nvarchar(40) NOT NULL CONSTRAINT PK_aces_season_status PRIMARY KEY,
        adj_calls      float        NOT NULL,
        adj_visitors   float        NOT NULL,
        days_calls     int          NOT NULL,
        days_visitors  int          NOT NULL,
        refreshed_utc  datetime2(0) NOT NULL
    );

DELETE FROM dbo.aces_season_forecast WHERE season_key = N'TAX_BILLS_2026';
DELETE FROM dbo.aces_season_status   WHERE season_key = N'TAX_BILLS_2026';

INSERT dbo.aces_season_forecast
    (season_key, target_date, season_day, calls, calls_low, calls_high,
     visitors, visitors_low, visitors_high, peak_hour_share, handle_seconds, built_from)
SELECT v.season_key, v.target_date, v.season_day, v.calls, v.calls_low, v.calls_high,
       v.visitors, v.visitors_low, v.visitors_high, v.peak_hour_share, v.handle_seconds,
       N'2023-2025 tax-bill seasons by mailing day; Oct 1-24, 2026 level; built 2026-10-28'
FROM (VALUES
    (N'TAX_BILLS_2026', '2026-11-02', 0, 138.64, 100.47, 196.74, 37.0, 26.04, 50.37, 0.1379, 237.6),
    (N'TAX_BILLS_2026', '2026-11-03', 1, 229.33, 166.2, 325.45, 60.16, 42.34, 81.89, 0.1519, 237.6),
    (N'TAX_BILLS_2026', '2026-11-04', 2, 246.43, 178.59, 349.71, 74.03, 52.1, 100.77, 0.1467, 237.6),
    (N'TAX_BILLS_2026', '2026-11-05', 3, 221.11, 160.24, 313.78, 70.48, 49.6, 95.94, 0.1447, 237.6),
    (N'TAX_BILLS_2026', '2026-11-06', 4, 188.12, 136.33, 266.96, 94.73, 66.67, 128.96, 0.1343, 237.6),
    (N'TAX_BILLS_2026', '2026-11-09', 5, 252.11, 182.71, 357.77, 101.8, 71.65, 138.58, 0.1364, 237.6),
    (N'TAX_BILLS_2026', '2026-11-10', 6, 211.67, 153.4, 300.39, 90.95, 64.01, 123.82, 0.1481, 237.6),
    (N'TAX_BILLS_2026', '2026-11-12', 7, 181.85, 131.79, 258.06, 71.46, 50.3, 97.28, 0.1429, 237.6),
    (N'TAX_BILLS_2026', '2026-11-13', 8, 148.3, 107.48, 210.46, 75.04, 52.81, 102.15, 0.1324, 237.6),
    (N'TAX_BILLS_2026', '2026-11-16', 9, 186.67, 135.28, 264.9, 70.19, 49.4, 95.55, 0.1333, 237.6),
    (N'TAX_BILLS_2026', '2026-11-17', 10, 157.7, 114.29, 223.8, 62.31, 43.85, 84.82, 0.1585, 237.6),
    (N'TAX_BILLS_2026', '2026-11-18', 11, 136.85, 99.18, 194.21, 65.45, 46.06, 89.09, 0.1429, 237.6),
    (N'TAX_BILLS_2026', '2026-11-19', 12, 143.24, 103.81, 203.28, 56.84, 40.01, 77.38, 0.1392, 237.6),
    (N'TAX_BILLS_2026', '2026-11-20', 13, 111.71, 80.96, 158.54, 65.52, 46.11, 89.19, 0.1449, 237.6),
    (N'TAX_BILLS_2026', '2026-11-23', 14, 148.3, 107.48, 210.46, 71.62, 50.4, 97.49, 0.1413, 237.6),
    (N'TAX_BILLS_2026', '2026-11-24', 15, 114.7, 83.13, 162.78, 57.52, 40.49, 78.31, 0.1548, 237.6),
    (N'TAX_BILLS_2026', '2026-11-25', 16, 105.62, 76.55, 149.89, 63.12, 44.42, 85.92, 0.141, 237.6),
    (N'TAX_BILLS_2026', '2026-11-30', 17, 139.2, 100.88, 197.54, 59.38, 41.79, 80.83, 0.1383, 237.6),
    (N'TAX_BILLS_2026', '2026-12-01', 18, 132.68, 96.16, 188.29, 61.35, 43.18, 83.51, 0.1512, 237.6),
    (N'TAX_BILLS_2026', '2026-12-02', 19, 117.34, 85.04, 166.52, 69.09, 48.62, 94.05, 0.1375, 237.6),
    (N'TAX_BILLS_2026', '2026-12-03', 20, 121.06, 87.73, 171.8, 54.11, 38.08, 73.66, 0.1375, 237.6),
    (N'TAX_BILLS_2026', '2026-12-04', 21, 103.72, 75.17, 147.19, 63.85, 44.94, 86.92, 0.1408, 237.6),
    (N'TAX_BILLS_2026', '2026-12-07', 22, 137.39, 99.57, 194.97, 67.06, 47.2, 91.28, 0.1354, 237.6),
    (N'TAX_BILLS_2026', '2026-12-08', 23, 116.92, 84.74, 165.93, 63.41, 44.63, 86.32, 0.1494, 237.6),
    (N'TAX_BILLS_2026', '2026-12-09', 24, 101.15, 73.3, 143.54, 62.2, 43.78, 84.67, 0.1481, 237.6),
    (N'TAX_BILLS_2026', '2026-12-10', 25, 97.48, 70.65, 138.34, 43.37, 30.52, 59.04, 0.1341, 237.6),
    (N'TAX_BILLS_2026', '2026-12-11', 26, 75.82, 54.95, 107.6, 43.72, 30.77, 59.52, 0.1389, 237.6),
    (N'TAX_BILLS_2026', '2026-12-14', 27, 99.06, 71.79, 140.58, 41.44, 29.17, 56.41, 0.1327, 237.6),
    (N'TAX_BILLS_2026', '2026-12-15', 28, 90.79, 65.79, 128.84, 33.97, 23.91, 46.25, 0.1573, 237.6),
    (N'TAX_BILLS_2026', '2026-12-16', 29, 75.64, 54.82, 107.34, 36.89, 25.96, 50.21, 0.1446, 237.6),
    (N'TAX_BILLS_2026', '2026-12-17', 30, 76.87, 55.71, 109.09, 31.42, 22.11, 42.77, 0.1446, 237.6),
    (N'TAX_BILLS_2026', '2026-12-18', 31, 65.5, 47.47, 92.95, 40.59, 28.57, 55.26, 0.137, 237.6),
    (N'TAX_BILLS_2026', '2026-12-21', 32, 90.79, 65.8, 128.85, 42.82, 30.13, 58.29, 0.14, 237.6),
    (N'TAX_BILLS_2026', '2026-12-22', 33, 73.7, 53.41, 104.59, 39.0, 27.45, 53.09, 0.1538, 237.6),
    (N'TAX_BILLS_2026', '2026-12-23', 34, 76.23, 55.25, 108.18, 49.16, 34.6, 66.93, 0.1412, 237.6)
) AS v (season_key, target_date, season_day, calls, calls_low, calls_high,
        visitors, visitors_low, visitors_high, peak_hour_share, handle_seconds);

INSERT dbo.aces_season_status (season_key, adj_calls, adj_visitors, days_calls, days_visitors, refreshed_utc)
VALUES (N'TAX_BILLS_2026', 1, 1, 0, 0, SYSUTCDATETIME());
DECLARE @loaded int = (SELECT COUNT(*) FROM dbo.aces_season_forecast WHERE season_key = N'TAX_BILLS_2026');
PRINT N'Season rows loaded: ' + CAST(@loaded AS nvarchar(10)) + N' (expected 35).';
GO

/* ---------------------------------------------------------------- 2. Erlang C */
CREATE OR ALTER FUNCTION dbo.aces_fn_erlang_agents
(
    @calls_per_hour float,
    @handle_seconds float,
    @target         float      -- share answered within 60 seconds, e.g. 0.80
)
RETURNS int
AS
BEGIN
    IF @calls_per_hour IS NULL OR @handle_seconds IS NULL OR @calls_per_hour <= 0 OR @handle_seconds <= 0
        RETURN 0;
    DECLARE @a float = @calls_per_hour * @handle_seconds / 3600.0;
    DECLARE @n int = FLOOR(@a) + 1;
    DECLARE @b float, @k int, @rho float, @wait float, @sl float;
    WHILE @n <= 200
    BEGIN
        SET @b = 1.0;
        SET @k = 1;
        WHILE @k <= @n
        BEGIN
            SET @b = @a * @b / (@k + @a * @b);     -- Erlang B, stable recursion
            SET @k = @k + 1;
        END;
        SET @rho = @a / @n;
        SET @wait = @b / (1 - @rho + @rho * @b);   -- Erlang C: chance a caller waits
        SET @sl = 1 - @wait * EXP(-(@n - @a) * 60.0 / @handle_seconds);
        IF @sl >= @target
            RETURN @n;
        SET @n = @n + 1;
    END;
    RETURN 200;
END;
GO

/* ---------------------------------------------------------------- 3. daily refresh */
DECLARE @v int = OBJECT_ID(N'dbo.aces_v_forecast_inbound_call');
IF @v IS NULL
    THROW 50001, 'dbo.aces_v_forecast_inbound_call was not found, so the refresh procedure was not created.', 1;

-- Which column holds the call's date. Literal lists, so catalog collations never clash.
DECLARE @col sysname, @expr nvarchar(max);   -- max, or the procedure text below is cut at 4,000 characters
SELECT TOP (1) @col = c.name
FROM sys.columns AS c
WHERE c.object_id = @v
  AND c.name IN (N'call_date', N'session_date', N'local_date', N'call_day', N'date', N'start_date', N'event_date')
ORDER BY CASE c.name WHEN N'call_date' THEN 1 WHEN N'session_date' THEN 2 WHEN N'local_date' THEN 3
                     WHEN N'call_day' THEN 4 WHEN N'date' THEN 5 WHEN N'start_date' THEN 6 ELSE 7 END;
IF @col IS NULL
    SELECT TOP (1) @col = c.name
    FROM sys.columns AS c JOIN sys.types AS t ON t.user_type_id = c.user_type_id
    WHERE c.object_id = @v AND t.name IN (N'date', N'datetime', N'datetime2', N'smalldatetime', N'datetimeoffset')
    ORDER BY c.column_id;
IF @col IS NULL
    SELECT TOP (1) @col = c.name
    FROM sys.columns AS c
    WHERE c.object_id = @v AND c.name LIKE N'%date%' AND c.name NOT LIKE N'%key%'
    ORDER BY c.column_id;

IF @col IS NOT NULL
    SET @expr = N'TRY_CAST(c.' + QUOTENAME(@col) + N' AS date)';
ELSE IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @v AND name = N'date_key')
    SET @expr = N'TRY_CONVERT(date, CONVERT(char(8), c.[date_key]), 112)';
ELSE
    THROW 50001, 'No date column found in dbo.aces_v_forecast_inbound_call, so the refresh procedure was not created. Send the view''s column list.', 1;

PRINT N'Call date taken from: ' + @expr;

-- Check against numbers we know (forecast-basis calls Oct 26 = 97 and Oct 27 = 72; visits 43 and 30).
DECLARE @check nvarchar(max) = N'
SELECT ''check: should read 97 calls, 43 visits'' AS what, (SELECT COUNT(*) FROM dbo.aces_v_forecast_inbound_call AS c WHERE ' + @expr + N' = ''2026-10-26'') AS calls,
       (SELECT COUNT(*) FROM dbo.fact_visitor WHERE visit_kpi_eligible = 1 AND TRY_CAST(visit_date AS date) = ''2026-10-26'') AS visits
UNION ALL
SELECT ''check: should read 72 calls, 30 visits'', (SELECT COUNT(*) FROM dbo.aces_v_forecast_inbound_call AS c WHERE ' + @expr + N' = ''2026-10-27''),
       (SELECT COUNT(*) FROM dbo.fact_visitor WHERE visit_kpi_eligible = 1 AND TRY_CAST(visit_date AS date) = ''2026-10-27'');';
EXEC sys.sp_executesql @check;

DECLARE @proc nvarchar(max) = N'
-- 53_TAX_SEASON_FORECAST.sql: run once a day after the loads, during a season.
-- Counts actual customer-facing calls and walk-ins for finished season days, then scales the
-- rest of the season to how it is running.
CREATE OR ALTER PROCEDURE dbo.aces_season_refresh
    @season_key nvarchar(40) = N''TAX_BILLS_2026''
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @today date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE ''Mountain Standard Time'' AS date);
    DECLARE @d0 date, @d1 date;
    SELECT @d0 = MIN(target_date), @d1 = MAX(target_date)
    FROM dbo.aces_season_forecast WHERE season_key = @season_key;
    IF @d0 IS NULL
        THROW 50001, ''No season rows for that season_key.'', 1;

    WITH ac AS (
        SELECT x.d, COUNT(*) AS n
        FROM dbo.aces_v_forecast_inbound_call AS c
        CROSS APPLY (SELECT ' + @expr + N' AS d) AS x
        WHERE x.d BETWEEN @d0 AND @d1 AND x.d < @today
        GROUP BY x.d
    ), av AS (
        SELECT x.d, COUNT(*) AS n
        FROM dbo.fact_visitor AS v
        CROSS APPLY (SELECT TRY_CAST(v.visit_date AS date) AS d) AS x
        WHERE v.visit_kpi_eligible = 1 AND x.d BETWEEN @d0 AND @d1 AND x.d < @today
        GROUP BY x.d
    )
    UPDATE f
       SET actual_calls          = CASE WHEN f.target_date < @today THEN ISNULL(ac.n, 0) END,
           actual_visitors       = CASE WHEN f.target_date < @today THEN ISNULL(av.n, 0) END,
           actuals_refreshed_utc = SYSUTCDATETIME()
      FROM dbo.aces_season_forecast AS f
      LEFT JOIN ac ON ac.d = f.target_date
      LEFT JOIN av ON av.d = f.target_date
     WHERE f.season_key = @season_key;

    DECLARE @nc int, @ac float, @ec float, @nv int, @av float, @ev float;
    SELECT @nc = COUNT(*), @ac = SUM(CAST(actual_calls AS float)), @ec = SUM(calls)
      FROM dbo.aces_season_forecast
     WHERE season_key = @season_key AND season_day >= 3
       AND actual_calls IS NOT NULL AND actual_calls >= 0.4 * calls;
    SELECT @nv = COUNT(*), @av = SUM(CAST(actual_visitors AS float)), @ev = SUM(visitors)
      FROM dbo.aces_season_forecast
     WHERE season_key = @season_key AND season_day >= 3
       AND actual_visitors IS NOT NULL AND actual_visitors >= 0.4 * visitors;

    DECLARE @adj_c float = 1, @adj_v float = 1;
    IF @nc > 0 SET @adj_c = 1 + (@ac / @ec - 1) * @nc / (@nc + 2.0);
    IF @nv > 0 SET @adj_v = 1 + (@av / @ev - 1) * @nv / (@nv + 2.0);
    SET @adj_c = CASE WHEN @adj_c < 0.6 THEN 0.6 WHEN @adj_c > 1.6 THEN 1.6 ELSE @adj_c END;
    SET @adj_v = CASE WHEN @adj_v < 0.6 THEN 0.6 WHEN @adj_v > 1.6 THEN 1.6 ELSE @adj_v END;

    UPDATE dbo.aces_season_status
       SET adj_calls = @adj_c, adj_visitors = @adj_v, days_calls = @nc, days_visitors = @nv,
           refreshed_utc = SYSUTCDATETIME()
     WHERE season_key = @season_key;
    IF @@ROWCOUNT = 0
        INSERT dbo.aces_season_status (season_key, adj_calls, adj_visitors, days_calls, days_visitors, refreshed_utc)
        VALUES (@season_key, @adj_c, @adj_v, @nc, @nv, SYSUTCDATETIME());

    -- Readout: finished season days so far, then the adjustment now in use.
    SELECT target_date, season_day,
           CAST(ROUND(calls, 0) AS int) AS season_calls, actual_calls,
           CAST(100.0 * actual_calls / NULLIF(calls, 0) AS decimal(6, 1)) AS calls_pct_of_season,
           CASE WHEN season_day < 3 THEN ''shown only (start of the wave)''
                WHEN actual_calls < 0.4 * calls THEN ''skipped: looks partly loaded''
                ELSE ''used'' END AS calls_status,
           CAST(ROUND(visitors, 0) AS int) AS season_visitors, actual_visitors,
           CAST(100.0 * actual_visitors / NULLIF(visitors, 0) AS decimal(6, 1)) AS visits_pct_of_season
      FROM dbo.aces_season_forecast
     WHERE season_key = @season_key AND target_date < @today
     ORDER BY target_date;

    SELECT season_key, CAST(adj_calls AS decimal(5, 2)) AS adj_calls, days_calls,
           CAST(adj_visitors AS decimal(5, 2)) AS adj_visitors, days_visitors, refreshed_utc
      FROM dbo.aces_season_status WHERE season_key = @season_key;
END';
EXEC sys.sp_executesql @proc;

IF USER_ID(N'ASR_CS_User') IS NOT NULL
    GRANT EXECUTE ON dbo.aces_season_refresh TO ASR_CS_User;
PRINT N'dbo.aces_season_refresh created.';
GO

/* ---------------------------------------------------------------- 4. ai_api.forecast */
IF OBJECT_ID(N'dbo.aces_season_forecast', N'U') IS NULL OR OBJECT_ID(N'dbo.aces_season_status', N'U') IS NULL
   OR OBJECT_ID(N'dbo.aces_fn_erlang_agents') IS NULL
    THROW 50001, 'The season tables or the Erlang function are missing (see the first error above). ai_api.forecast was not changed.', 1;

DECLARE @view int = OBJECT_ID(N'ai_read.forecast_current_daily');
DECLARE @tbl  int = OBJECT_ID(N'dbo.aces_forecast_daily');
IF @view IS NULL
    THROW 50001, 'ai_read.forecast_current_daily was not found. ai_api.forecast was not changed.', 1;

DECLARE @view_has_95 bit =
    CASE WHEN (SELECT COUNT(*) FROM sys.columns WHERE object_id = @view
               AND name IN (N'calls_low95', N'calls_high95', N'visitors_low95', N'visitors_high95')) = 4
         THEN 1 ELSE 0 END;
DECLARE @table_has_95 bit =
    CASE WHEN (SELECT COUNT(*) FROM sys.columns WHERE object_id = @tbl
               AND name IN (N'calls_low95', N'calls_high95', N'visitors_low95', N'visitors_high95')) = 4
         THEN 1 ELSE 0 END;
DECLARE @view_has_sl bit =
    CASE WHEN EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @view AND name = N'peak_phone_staff_service_level')
         THEN 1 ELSE 0 END;
DECLARE @table_has_sl bit =
    CASE WHEN EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @tbl AND name = N'peak_phone_staff_service_level')
         THEN 1 ELSE 0 END;
DECLARE @view_has_run bit =
    CASE WHEN EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @view AND name = N'run_id') THEN 1 ELSE 0 END;

DECLARE @c95 nvarchar(400), @csl nvarchar(200), @join bit = 0, @path nvarchar(400) = N'';
IF @view_has_95 = 1
    SELECT @c95 = N'f.calls_low95, f.calls_high95, f.visitors_low95, f.visitors_high95',
           @path = @path + N'95% ranges from the view; ';
ELSE IF @view_has_run = 1 AND @table_has_95 = 1
    SELECT @c95 = N'd.calls_low95, d.calls_high95, d.visitors_low95, d.visitors_high95',
           @join = 1, @path = @path + N'95% ranges joined from dbo.aces_forecast_daily; ';
ELSE
    THROW 50001, 'The 95% columns were not found. ai_api.forecast was not changed.', 1;
IF @view_has_sl = 1
    SELECT @csl = N'f.peak_phone_staff_service_level', @path = @path + N'Erlang C number from the view.';
ELSE IF @view_has_run = 1 AND @table_has_sl = 1
    SELECT @csl = N'd.peak_phone_staff_service_level', @join = 1,
           @path = @path + N'Erlang C number joined from dbo.aces_forecast_daily.';
ELSE
    THROW 50001, 'peak_phone_staff_service_level was not found. ai_api.forecast was not changed.', 1;

DECLARE @src nvarchar(max) = N'
        SELECT f.target_date, f.as_of_date,
               f.calls, f.calls_low80, f.calls_high80,
               f.visitors, f.visitors_low80, f.visitors_high80,
               f.phone_staff, f.peak_phone_staff, f.review_status,
               ' + @c95 + N',
               ' + @csl + N' AS peak_phone_staff_service_level
        FROM ai_read.forecast_current_daily AS f'
    + CASE WHEN @join = 1 THEN N'
        LEFT JOIN dbo.aces_forecast_daily AS d
               ON d.run_id = f.run_id AND d.target_date = f.target_date' ELSE N'' END + N'
        WHERE f.target_date BETWEEN @start_date AND @end_date';

DECLARE @sql nvarchar(max) = N'
-- Current daily forecast for a date range (up to 120 days).
-- 48_FORECAST_95_RANGES.sql: 95% ranges added after review_status.
-- 51_FORECAST_95_RANGE_FIX.sql: each side of the 95% range is at least 1.529369 times that
--   side of the 80% range (the forecast job widens the 80% range by lead time, not the 95%).
-- 52_FORECAST_PHONES_NEEDED.sql: phones_needed_peak_hour (Erlang C, 80% within 60 s).
-- 53_TAX_SEASON_FORECAST.sql: season_ columns for tax-bill season dates, from
--   dbo.aces_season_forecast scaled by dbo.aces_season_status (NULL on other dates).
ALTER PROCEDURE [ai_api].[forecast]
    @start_date date,
    @end_date   date
AS
BEGIN
    SET NOCOUNT ON;

    IF @start_date IS NULL OR @end_date IS NULL OR @end_date < @start_date
        THROW 50001, ''start_date and end_date are required, and end_date must be on or after start_date.'', 1;
    IF DATEDIFF(day, @start_date, @end_date) > 120
        THROW 50001, ''The forecast range is limited to 120 days.'', 1;

    INSERT ai_api.tool_log (proc_name, params)
    VALUES (OBJECT_NAME(@@PROCID), CONCAT(''start='', @start_date, '' end='', @end_date));

    -- 95% vs 80% width on a normal curve: 1.959964 / 1.281552
    DECLARE @k decimal(9,6) = 1.529369;

    WITH src AS (' + @src + N'
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
           season_days_checked  = CASE WHEN sf.target_date IS NOT NULL THEN sa.days_c END
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
                 WHERE x.target_date = s.target_date
                 ORDER BY x.season_key DESC) AS sf
    OUTER APPLY (SELECT ISNULL(MAX(st.adj_calls), 1.0)    AS adj_c,
                        ISNULL(MAX(st.adj_visitors), 1.0) AS adj_v,
                        ISNULL(MAX(st.days_calls), 0)     AS days_c
                 FROM dbo.aces_season_status AS st
                 WHERE st.season_key = sf.season_key) AS sa
    ORDER BY s.target_date;
END';

EXEC sys.sp_executesql @sql;
PRINT N'ai_api.forecast updated (53). ' + @path;
GO

/* ---------------------------------------------------------------- 5. checks */
EXEC dbo.aces_season_refresh;   -- before Nov 2: no finished season days yet, adjustment 1.00
EXEC ai_api.forecast @start_date = '2026-11-02', @end_date = '2026-11-13';
