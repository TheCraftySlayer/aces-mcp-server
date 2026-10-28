/*
  51_FORECAST_95_RANGE_FIX.sql

  Fixes the 95% forecast ranges that ai_api.forecast returns to A.C.E.S.

  The problem: the forecast job widens the 80% range for dates further ahead,
  but never widens the 95% range. It only keeps the 95% range from being
  narrower than the 80% range, so once the widened 80% range catches up, the
  95% range sits right on top of it (the 95% low equals the 80% low).

  The fix, at read time: each side of the 95% range is at least 1.529369 times
  that side of the 80% range, measured from the forecast. 1.529369 is how much
  wider a 95% range is than an 80% range on a normal curve (1.959964 / 1.281552).
  A stored 95% value that is already wider is kept. The 80% ranges, the
  forecast numbers and the staffing columns are not touched.

  Limits:
    - Only what ai_api.forecast returns changes (the A.C.E.S. chat and charts).
      dbo.aces_forecast_daily, the desktop app and report exports still show the
      job's original 95% values until the forecast code is fixed.
    - The low side will likely be wider than reality, since actuals rarely come
      in far under the forecast. For a 95% range, too wide is the safer miss.

  Safe to rerun. Permissions on the procedure are kept (ALTER keeps grants).
  To undo, rerun 48_FORECAST_95_RANGES.sql.

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

-- Same two ways in as 48. Literal lists, so catalog collations never clash.
DECLARE @view_has_95 bit =
    CASE WHEN (SELECT COUNT(*) FROM sys.columns WHERE object_id = @view
               AND name IN (N'calls_low95', N'calls_high95', N'visitors_low95', N'visitors_high95')) = 4
         THEN 1 ELSE 0 END;
DECLARE @view_has_run bit =
    CASE WHEN EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @view AND name = N'run_id') THEN 1 ELSE 0 END;
DECLARE @table_has_95 bit =
    CASE WHEN (SELECT COUNT(*) FROM sys.columns WHERE object_id = @tbl
               AND name IN (N'calls_low95', N'calls_high95', N'visitors_low95', N'visitors_high95')) = 4
         THEN 1 ELSE 0 END;

DECLARE @src nvarchar(max);
DECLARE @path nvarchar(200);

IF @view_has_95 = 1
BEGIN
    SET @path = N'A: 95% columns read from ai_read.forecast_current_daily';
    SET @src = N'
        SELECT f.target_date, f.as_of_date,
               f.calls, f.calls_low80, f.calls_high80,
               f.visitors, f.visitors_low80, f.visitors_high80,
               f.phone_staff, f.peak_phone_staff, f.review_status,
               f.calls_low95, f.calls_high95, f.visitors_low95, f.visitors_high95
        FROM ai_read.forecast_current_daily AS f
        WHERE f.target_date BETWEEN @start_date AND @end_date';
END
ELSE IF @view_has_run = 1 AND @table_has_95 = 1
BEGIN
    SET @path = N'B: 95% columns joined from dbo.aces_forecast_daily on run_id and target_date';
    SET @src = N'
        SELECT f.target_date, f.as_of_date,
               f.calls, f.calls_low80, f.calls_high80,
               f.visitors, f.visitors_low80, f.visitors_high80,
               f.phone_staff, f.peak_phone_staff, f.review_status,
               d.calls_low95, d.calls_high95, d.visitors_low95, d.visitors_high95
        FROM ai_read.forecast_current_daily AS f
        LEFT JOIN dbo.aces_forecast_daily AS d
               ON d.run_id = f.run_id AND d.target_date = f.target_date
        WHERE f.target_date BETWEEN @start_date AND @end_date';
END
ELSE
    THROW 50001, 'ai_read.forecast_current_daily has neither the 95% columns nor run_id (or dbo.aces_forecast_daily lacks the 95% columns). Nothing changed.', 1;

DECLARE @sql nvarchar(max) = N'
-- Current daily forecast for a date range (up to 120 days).
-- 48_FORECAST_95_RANGES.sql: 95% ranges added after review_status.
-- 51_FORECAST_95_RANGE_FIX.sql: each side of the 95% range is at least
--   1.529369 times that side of the 80% range (the forecast job widens the
--   80% range for dates further ahead but not the 95% range).
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
           visitors_high95 = CASE WHEN w.vh > s.visitors_high95 THEN w.vh ELSE s.visitors_high95 END
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
PRINT N'ai_api.forecast updated (51). Path ' + @path + N'.';

-- Check: the next two weeks. The 95% columns are the last four.
DECLARE @today date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Mountain Standard Time' AS date);
DECLARE @end date = DATEADD(day, 13, @today);
EXEC ai_api.forecast @start_date = @today, @end_date = @end;
