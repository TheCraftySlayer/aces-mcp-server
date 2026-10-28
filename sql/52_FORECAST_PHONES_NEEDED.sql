/*
  52_FORECAST_PHONES_NEEDED.sql

  Adds the Erlang C phone number to what ai_api.forecast returns, as a new
  last column: phones_needed_peak_hour.

  Why: phone_staff and peak_phone_staff use the old 1.6 method (workload
  divided by 85% occupancy). ServiceLevelStaffing.cs says that method
  understates the people needed on a small team, and on quiet Fridays it now
  says 1. The forecast job already stores the Erlang C number for every day in
  peak_phone_staff_service_level: people on the phones in the busiest hour so
  that 80% of calls are answered within 60 seconds, at the forecast volume.
  It does NOT include time away from calls (breaks, walk-ins), so the people
  scheduled has to be higher.

  Changes nothing else: same inputs, checks, tool_log entry and rows, and the
  95% ranges from 51 stay. phone_staff and peak_phone_staff are still returned
  so nothing that reads them breaks.

  Safe to rerun. Permissions on the procedure are kept (ALTER keeps grants).
  To undo, rerun 51_FORECAST_95_RANGE_FIX.sql.

  Run on:
    - ASRCustomerService on asbcdbbernt (as ASR_CS_Owner)
    - Laptop ACES_Test on ASLT260012\SQLEXPRESS
    - Azure ACES_Test on test-aces2 once the subscription is back
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;

DECLARE @view int = OBJECT_ID(N'ai_read.forecast_current_daily');
DECLARE @tbl  int = OBJECT_ID(N'dbo.aces_forecast_daily');

IF @view IS NULL
    THROW 50001, 'ai_read.forecast_current_daily was not found. Nothing changed.', 1;

-- Literal lists, so catalog collations never clash.
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

DECLARE @c95  nvarchar(400);
DECLARE @csl  nvarchar(200);
DECLARE @join bit = 0;
DECLARE @path nvarchar(400) = N'';

IF @view_has_95 = 1
    SELECT @c95 = N'f.calls_low95, f.calls_high95, f.visitors_low95, f.visitors_high95',
           @path = @path + N'95% ranges from the view; ';
ELSE IF @view_has_run = 1 AND @table_has_95 = 1
    SELECT @c95 = N'd.calls_low95, d.calls_high95, d.visitors_low95, d.visitors_high95',
           @join = 1, @path = @path + N'95% ranges joined from dbo.aces_forecast_daily; ';
ELSE
    THROW 50001, 'The 95% columns were not found in the view or (with run_id) in dbo.aces_forecast_daily. Nothing changed.', 1;

IF @view_has_sl = 1
    SELECT @csl = N'f.peak_phone_staff_service_level',
           @path = @path + N'Erlang C number from the view.';
ELSE IF @view_has_run = 1 AND @table_has_sl = 1
    SELECT @csl = N'd.peak_phone_staff_service_level',
           @join = 1, @path = @path + N'Erlang C number joined from dbo.aces_forecast_daily.';
ELSE
    THROW 50001, 'peak_phone_staff_service_level was not found in the view or (with run_id) in dbo.aces_forecast_daily. Nothing changed.', 1;

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
-- 51_FORECAST_95_RANGE_FIX.sql: each side of the 95% range is at least
--   1.529369 times that side of the 80% range (the forecast job widens the
--   80% range for dates further ahead but not the 95% range).
-- 52_FORECAST_PHONES_NEEDED.sql: phones_needed_peak_hour added last: Erlang C
--   people on the phones in the busiest hour to answer 80% of calls within
--   60 seconds (stored as peak_phone_staff_service_level). phone_staff and
--   peak_phone_staff use the older occupancy method, which understates small teams.
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
           phones_needed_peak_hour = s.peak_phone_staff_service_level
    FROM src AS s
    CROSS APPLY (SELECT
        cl = CAST(ROUND(IIF(s.calls - @k * (s.calls - s.calls_low80) < 0, 0,
                            s.calls - @k * (s.calls - s.calls_low80)), 0) AS int),
        ch = CAST(ROUND(s.calls + @k * (s.calls_high80 - s.calls), 0) AS int),
        vl = CAST(ROUND(IIF(s.visitors - @k * (s.visitors - s.visitors_low80) < 0, 0,
                            s.visitors - @k * (s.visitors - s.visitors_low80)), 0) AS int),
        vh = CAST(ROUND(s.visitors + @k * (s.visitors_high80 - s.visitors), 0) AS int)
    ) AS w
    ORDER BY s.target_date;
END';

EXEC sys.sp_executesql @sql;
PRINT N'ai_api.forecast updated (52). ' + @path;

-- Check: the next two weeks. phones_needed_peak_hour is the last column.
DECLARE @today date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Mountain Standard Time' AS date);
DECLARE @end date = DATEADD(day, 13, @today);
EXEC ai_api.forecast @start_date = @today, @end_date = @end;
