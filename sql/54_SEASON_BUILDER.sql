/*
  54_SEASON_BUILDER.sql

  Season forecasts that are rebuilt from a mailing date, starting with the 2027 Notice of Value
  season. Builds on 53 (the tax-bill season keeps working as it is).

  The Notice of Value shape (Oct 28, 2026):
    - The last five springs (2022 to 2026; calls on file from 2024) lined up two ways: by the day
      notices land (3 calendar days after mailing, next open day) for the first 10 open days, then
      by the protest deadline (30 days after mailing, next open day) up to 10 open days after it.
    - Each day is a multiple of that spring's normal level (20 open days ending 3 open days before
      mailing), weekday adjusted, averaged over the years.
    - Tested: each spring predicted from the others missed by 41 to 79 calls a day, against 105 to
      202 for a normal-level plan. 2026 customer-facing calls from 2024 and 2025 alone missed by 53
      a day, against 138 for a normal plan and 82 for the regular forecast running one day ahead.
    - The normal level is the weak part: spring runs 1.6 to 2.0 times the October before. The 2027
      level starts as a guess (139.7 customer-facing calls and 64.6 walk-ins a day, about
      1.8 times this October) with a wide range (calls x0.622 to x1.432). 20 hidden warm-up days in
      March let the daily refresh lock in the real level before the notices go out.

  What this script does (run as ASR_CS_Owner; safe to rerun):
    1. Tables: dbo.aces_season_type (one row per kind of season), dbo.aces_season_curve (the shape),
       dbo.aces_season_weekday (weekday pattern), dbo.aces_season (one row per season: name, basis,
       mailing, landing and deadline dates, normal level). Adds show_in_forecast to
       dbo.aces_season_forecast (0 = warm-up day: used for the level, not shown).
    2. dbo.aces_season_build: builds a season's days from a mailing date. To move the 2027 season
       when the date is set, run it again with the new date, e.g.
         EXEC dbo.aces_season_build @season_key = N'NOV_2027', @season_type = N'NOV',
              @mailing_date = '2027-03-29', @base_calls = 139.7, @base_visits = 64.6,
              @season_name = N'Notice of Value season 2027';
       Closed days come from @closed_dates (2027 holidays by default); add any others.
    3. dbo.aces_season_refresh: with no argument it refreshes every season that is running or
       warming up. Warm-up days count toward the adjustment, so March sets the level (warm-up
       days over 1.6 times normal are skipped as event days).
    4. ai_api.season_outlook: season days for any date range (up to 200 days), so A.C.E.S. can show
       April now, before the regular forecast reaches it.
    5. ai_api.forecast: same as 53, hides warm-up days, and adds season_name and season_basis.
    6. Builds NOV_2027 for a Thu Apr 1, 2027 mailing (the date on the calendar now).

  To undo the ai_api.forecast change, rerun 53. If any part prints an error, stop and send the messages.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

/* ---------------------------------------------------------------- 1. tables */
IF OBJECT_ID(N'dbo.aces_season_forecast', N'U') IS NULL OR OBJECT_ID(N'dbo.aces_fn_erlang_agents') IS NULL
    THROW 50001, 'Run 53_TAX_SEASON_FORECAST.sql first.', 1;

IF COL_LENGTH(N'dbo.aces_season_forecast', N'show_in_forecast') IS NULL
    ALTER TABLE dbo.aces_season_forecast
        ADD show_in_forecast bit NOT NULL CONSTRAINT DF_aces_season_forecast_show DEFAULT 1;

IF OBJECT_ID(N'dbo.aces_season_type', N'U') IS NULL
    CREATE TABLE dbo.aces_season_type (
        season_type          nvarchar(20)  NOT NULL CONSTRAINT PK_aces_season_type PRIMARY KEY,
        basis                nvarchar(200) NOT NULL,   -- what the shape is built from, in plain words
        jump_offset_days     int           NOT NULL,   -- calendar days from mailing to landing
        deadline_offset_days int           NOT NULL,   -- calendar days from mailing to the deadline
        j_cutoff             int           NOT NULL,   -- open days counted from landing; after that, from the deadline
        warmup_days          int           NOT NULL,
        after_deadline_days  int           NOT NULL,
        calls_low            float         NOT NULL,   -- 80% range multipliers
        calls_high           float         NOT NULL,
        visits_low           float         NOT NULL,
        visits_high          float         NOT NULL
    );

IF OBJECT_ID(N'dbo.aces_season_curve', N'U') IS NULL
    CREATE TABLE dbo.aces_season_curve (
        season_type nvarchar(20) NOT NULL,
        pos_kind    char(1)      NOT NULL,   -- j: open days from landing; k: open days from the deadline
        pos         int          NOT NULL,
        mult_calls  float        NOT NULL,
        mult_visits float        NOT NULL,
        CONSTRAINT PK_aces_season_curve PRIMARY KEY (season_type, pos_kind, pos)
    );

IF OBJECT_ID(N'dbo.aces_season_weekday', N'U') IS NULL
    CREATE TABLE dbo.aces_season_weekday (
        season_type nvarchar(20) NOT NULL,
        weekday     int          NOT NULL,   -- 0 = Monday ... 4 = Friday
        f_calls     float        NOT NULL,
        f_visits    float        NOT NULL,
        CONSTRAINT PK_aces_season_weekday PRIMARY KEY (season_type, weekday)
    );

IF OBJECT_ID(N'dbo.aces_season', N'U') IS NULL
    CREATE TABLE dbo.aces_season (
        season_key    nvarchar(40)  NOT NULL CONSTRAINT PK_aces_season PRIMARY KEY,
        season_type   nvarchar(20)  NOT NULL,
        season_name   nvarchar(80)  NOT NULL,
        season_basis  nvarchar(200) NOT NULL,
        mailing_date  date          NULL,
        landing_date  date          NULL,
        deadline_date date          NULL,
        base_calls    float         NULL,
        base_visits   float         NULL,
        built_utc     datetime2(0)  NOT NULL
    );
GO

/* ---------------------------------------------------------------- 2. the Notice of Value shape */
DELETE FROM dbo.aces_season_curve   WHERE season_type = N'NOV';
DELETE FROM dbo.aces_season_weekday WHERE season_type = N'NOV';
DELETE FROM dbo.aces_season_type    WHERE season_type = N'NOV';

INSERT dbo.aces_season_type
    (season_type, basis, jump_offset_days, deadline_offset_days, j_cutoff, warmup_days, after_deadline_days,
     calls_low, calls_high, visits_low, visits_high)
VALUES
    (N'NOV', N'the last five Notice of Value seasons (2022 to 2026; calls from 2024 to 2026), lined up by when notices landed and by the protest deadline',
     3, 30, 9, 20, 10, 0.622, 1.432, 0.72, 1.3);

INSERT dbo.aces_season_curve (season_type, pos_kind, pos, mult_calls, mult_visits)
VALUES
    (N'NOV', 'j', -3, 0.8721, 1.0722),
    (N'NOV', 'j', -2, 1.0624, 1.0259),
    (N'NOV', 'j', -1, 1.0861, 1.2198),
    (N'NOV', 'j', 0, 3.5014, 2.0053),
    (N'NOV', 'j', 1, 3.152, 2.1782),
    (N'NOV', 'j', 2, 2.7006, 2.144),
    (N'NOV', 'j', 3, 2.3275, 2.3681),
    (N'NOV', 'j', 4, 2.1839, 2.1636),
    (N'NOV', 'j', 5, 2.0095, 2.0576),
    (N'NOV', 'j', 6, 1.8457, 1.8808),
    (N'NOV', 'j', 7, 1.776, 1.8844),
    (N'NOV', 'j', 8, 1.6669, 1.8346),
    (N'NOV', 'j', 9, 1.6107, 1.8678),
    (N'NOV', 'k', -9, 1.6836, 1.6296),
    (N'NOV', 'k', -8, 1.7026, 1.822),
    (N'NOV', 'k', -7, 1.7295, 1.8971),
    (N'NOV', 'k', -6, 1.734, 1.9655),
    (N'NOV', 'k', -5, 1.7037, 2.1138),
    (N'NOV', 'k', -4, 1.6796, 2.3185),
    (N'NOV', 'k', -3, 1.7556, 2.4687),
    (N'NOV', 'k', -2, 1.8086, 2.5673),
    (N'NOV', 'k', -1, 2.0692, 2.9176),
    (N'NOV', 'k', 0, 1.8154, 2.9746),
    (N'NOV', 'k', 1, 1.1924, 0.9252),
    (N'NOV', 'k', 2, 1.2244, 0.9362),
    (N'NOV', 'k', 3, 1.2634, 0.8923),
    (N'NOV', 'k', 4, 1.3059, 0.9165),
    (N'NOV', 'k', 5, 1.2319, 0.8444),
    (N'NOV', 'k', 6, 1.1747, 0.9038),
    (N'NOV', 'k', 7, 1.1422, 0.8975),
    (N'NOV', 'k', 8, 1.1684, 0.9526),
    (N'NOV', 'k', 9, 1.165, 0.9128),
    (N'NOV', 'k', 10, 1.0684, 0.8851),
    (N'NOV', 'k', 11, 1.0699, 0.8839),
    (N'NOV', 'k', 12, 1.092, 0.9088),
    (N'NOV', 'k', 13, 1.1344, 0.9289),
    (N'NOV', 'k', 14, 1.0574, 0.9504),
    (N'NOV', 'k', 15, 0.9654, 0.9038);

INSERT dbo.aces_season_weekday (season_type, weekday, f_calls, f_visits)
VALUES
    (N'NOV', 0, 1.1154, 1.1686),
    (N'NOV', 1, 1.0554, 0.9801),
    (N'NOV', 2, 1.0143, 0.867),
    (N'NOV', 3, 0.9263, 0.9466),
    (N'NOV', 4, 0.8887, 1.0377);

-- The tax-bill season from 53, so the forecast can name it.
IF NOT EXISTS (SELECT 1 FROM dbo.aces_season WHERE season_key = N'TAX_BILLS_2026')
    INSERT dbo.aces_season (season_key, season_type, season_name, season_basis, mailing_date, landing_date,
                            deadline_date, base_calls, base_visits, built_utc)
    VALUES (N'TAX_BILLS_2026', N'TAX', N'Tax-bill season 2026', N'the last three tax-bill seasons (2023 to 2025)',
            '2026-11-02', '2026-11-03', '2026-11-10', 76.3, 39.8, SYSUTCDATETIME());

DECLARE @curve_rows int = (SELECT COUNT(*) FROM dbo.aces_season_curve WHERE season_type = N'NOV');
PRINT N'Notice of Value shape loaded: ' + CAST(@curve_rows AS nvarchar(10)) + N' positions (expected 38).';
GO

/* ---------------------------------------------------------------- 3. build a season from a mailing date */
CREATE OR ALTER PROCEDURE dbo.aces_season_build
    @season_key      nvarchar(40),
    @season_type     nvarchar(20),
    @mailing_date    date,
    @base_calls      float,          -- normal customer-facing calls a day before the season
    @base_visits     float,          -- normal walk-ins a day before the season
    @season_name     nvarchar(80),
    @closed_dates    nvarchar(max) = N'2027-03-26,2027-05-31,2027-06-18,2027-07-05',
    @peak_hour_share float = 0.145,
    @handle_seconds  float = 237.6
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @basis nvarchar(200), @jump_offset int, @deadline_offset int, @j_cutoff int, @warmup int,
            @after int, @cl float, @ch float, @vl float, @vh float;
    SELECT @basis = basis, @jump_offset = jump_offset_days, @deadline_offset = deadline_offset_days,
           @j_cutoff = j_cutoff, @warmup = warmup_days, @after = after_deadline_days,
           @cl = calls_low, @ch = calls_high, @vl = visits_low, @vh = visits_high
      FROM dbo.aces_season_type WHERE season_type = @season_type;
    IF @basis IS NULL
        THROW 50001, 'Unknown season_type. Nothing changed.', 1;
    IF @season_key IS NULL OR @mailing_date IS NULL OR @base_calls IS NULL OR @base_calls <= 0
       OR @base_visits IS NULL OR @base_visits < 0 OR @season_name IS NULL
        THROW 50001, 'season_key, mailing_date, base_calls, base_visits and season_name are required. Nothing changed.', 1;

    -- Closed days, from a comma-separated list of dates.
    DECLARE @closed TABLE (d date PRIMARY KEY);
    DECLARE @list nvarchar(max) = ISNULL(@closed_dates, N'') + N',', @at int, @item nvarchar(40), @cd date;
    WHILE LEN(@list) > 0
    BEGIN
        SET @at = CHARINDEX(N',', @list);
        SET @item = LTRIM(RTRIM(LEFT(@list, @at - 1)));
        SET @list = SUBSTRING(@list, @at + 1, LEN(@list) + 1);
        SET @cd = TRY_CAST(@item AS date);
        IF @cd IS NOT NULL AND NOT EXISTS (SELECT 1 FROM @closed WHERE d = @cd)
            INSERT @closed (d) VALUES (@cd);
    END;

    -- Open days around the season, numbered in order.
    DECLARE @days TABLE (d date PRIMARY KEY, n int NOT NULL, wd int NOT NULL);
    DECLARE @d date = DATEADD(day, -70, @mailing_date),
            @last date = DATEADD(day, @deadline_offset + 50, @mailing_date),
            @n int = 0, @wd int;
    WHILE @d <= @last
    BEGIN
        SET @wd = DATEDIFF(day, '19000101', @d) % 7;     -- 0 = Monday
        IF @wd < 5 AND NOT EXISTS (SELECT 1 FROM @closed WHERE d = @d)
        BEGIN
            SET @n = @n + 1;
            INSERT @days (d, n, wd) VALUES (@d, @n, @wd);
        END;
        SET @d = DATEADD(day, 1, @d);
    END;

    DECLARE @landing  date = (SELECT MIN(d) FROM @days WHERE d >= DATEADD(day, @jump_offset, @mailing_date));
    DECLARE @deadline date = (SELECT MIN(d) FROM @days WHERE d >= DATEADD(day, @deadline_offset, @mailing_date));
    DECLARE @nl int = (SELECT n FROM @days WHERE d = @landing);
    DECLARE @nd int = (SELECT n FROM @days WHERE d = @deadline);
    DECLARE @jmin int, @kmin int, @kmax int;
    SELECT @jmin = MIN(pos) FROM dbo.aces_season_curve WHERE season_type = @season_type AND pos_kind = 'j';
    SELECT @kmin = MIN(pos), @kmax = MAX(pos) FROM dbo.aces_season_curve WHERE season_type = @season_type AND pos_kind = 'k';

    BEGIN TRANSACTION;
        DELETE FROM dbo.aces_season_forecast WHERE season_key = @season_key;

        -- Season days: from 3 open days before landing to @after open days past the deadline.
        INSERT dbo.aces_season_forecast
            (season_key, target_date, season_day, calls, calls_low, calls_high, visitors, visitors_low,
             visitors_high, peak_hour_share, handle_seconds, built_from, show_in_forecast)
        SELECT @season_key, x.d, x.j,
               @base_calls * w.f_calls * c.mult_calls,
               @base_calls * w.f_calls * c.mult_calls * @cl,
               @base_calls * w.f_calls * c.mult_calls * @ch,
               @base_visits * w.f_visits * c.mult_visits,
               @base_visits * w.f_visits * c.mult_visits * @vl,
               @base_visits * w.f_visits * c.mult_visits * @vh,
               @peak_hour_share, @handle_seconds, @basis, 1
          FROM (SELECT d, wd, n - @nl AS j, n - @nd AS k FROM @days) AS x
         CROSS APPLY (SELECT CASE WHEN x.j <= @j_cutoff THEN 'j' ELSE 'k' END AS kind,
                             CASE WHEN x.j <= @j_cutoff THEN x.j
                                  WHEN x.k < @kmin THEN @kmin
                                  WHEN x.k > @kmax THEN @kmax
                                  ELSE x.k END AS p) AS pp
          JOIN dbo.aces_season_curve   AS c ON c.season_type = @season_type AND c.pos_kind = pp.kind AND c.pos = pp.p
          JOIN dbo.aces_season_weekday AS w ON w.season_type = @season_type AND w.weekday = x.wd
         WHERE x.j >= @jmin AND x.k <= @after;

        -- Warm-up days before the season: the normal level only, not shown. The refresh compares
        -- them with actuals to lock in the level before the notices go out.
        INSERT dbo.aces_season_forecast
            (season_key, target_date, season_day, calls, calls_low, calls_high, visitors, visitors_low,
             visitors_high, peak_hour_share, handle_seconds, built_from, show_in_forecast)
        SELECT @season_key, x.d, x.j,
               @base_calls * w.f_calls, @base_calls * w.f_calls, @base_calls * w.f_calls,
               @base_visits * w.f_visits, @base_visits * w.f_visits, @base_visits * w.f_visits,
               @peak_hour_share, @handle_seconds, @basis, 0
          FROM (SELECT d, wd, n - @nl AS j FROM @days) AS x
          JOIN dbo.aces_season_weekday AS w ON w.season_type = @season_type AND w.weekday = x.wd
         WHERE x.j >= @jmin - 1 - @warmup AND x.j <= @jmin - 2;

        DELETE FROM dbo.aces_season WHERE season_key = @season_key;
        INSERT dbo.aces_season (season_key, season_type, season_name, season_basis, mailing_date, landing_date,
                                deadline_date, base_calls, base_visits, built_utc)
        VALUES (@season_key, @season_type, @season_name, @basis, @mailing_date, @landing, @deadline,
                @base_calls, @base_visits, SYSUTCDATETIME());

        DELETE FROM dbo.aces_season_status WHERE season_key = @season_key;
        INSERT dbo.aces_season_status (season_key, adj_calls, adj_visitors, days_calls, days_visitors, refreshed_utc)
        VALUES (@season_key, 1, 1, 0, 0, SYSUTCDATETIME());
    COMMIT;

    PRINT CONCAT(N'Built ', @season_key, N': mailed ', CONVERT(char(10), @mailing_date, 23),
                 N', notices land ', CONVERT(char(10), @landing, 23),
                 N', deadline ', CONVERT(char(10), @deadline, 23), N'.');

    -- The season by week (warm-up days left out).
    SELECT DATEADD(day, -(DATEDIFF(day, '19000101', target_date) % 7), target_date) AS week_of,
           COUNT(*) AS open_days,
           CAST(ROUND(AVG(calls), 0) AS int)      AS calls_per_day,
           CAST(ROUND(AVG(calls_low), 0) AS int)  AS calls_low,
           CAST(ROUND(AVG(calls_high), 0) AS int) AS calls_high,
           CAST(ROUND(AVG(visitors), 0) AS int)   AS walk_ins_per_day,
           MAX(dbo.aces_fn_erlang_agents(calls * peak_hour_share, handle_seconds, 0.80))      AS phones_busiest_hour,
           MAX(dbo.aces_fn_erlang_agents(calls_high * peak_hour_share, handle_seconds, 0.80)) AS phones_on_a_high_day
      FROM dbo.aces_season_forecast
     WHERE season_key = @season_key AND show_in_forecast = 1
     GROUP BY DATEADD(day, -(DATEDIFF(day, '19000101', target_date) % 7), target_date)
     ORDER BY week_of;

    -- Pick up any warm-up or season days that have already happened.
    EXEC dbo.aces_season_refresh @season_key = @season_key;
END;
GO

/* ---------------------------------------------------------------- 4. daily refresh, every running season */
IF COL_LENGTH(N'dbo.aces_v_forecast_inbound_call', N'event_date') IS NULL
    THROW 50001, 'dbo.aces_v_forecast_inbound_call has no event_date column, so the refresh was not changed.', 1;
GO
CREATE OR ALTER PROCEDURE dbo.aces_season_refresh
    @season_key nvarchar(40) = NULL      -- NULL: every season that is running or warming up today
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @today date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Mountain Standard Time' AS date);

    IF @season_key IS NULL
    BEGIN
        DECLARE @k nvarchar(40) = N'', @done int = 0;
        WHILE 1 = 1
        BEGIN
            SELECT @k = MIN(a.season_key)
              FROM (SELECT season_key FROM dbo.aces_season_forecast
                     GROUP BY season_key
                    HAVING MIN(target_date) <= DATEADD(day, 1, @today)
                       AND MAX(target_date) >= DATEADD(day, -7, @today)) AS a
             WHERE a.season_key > @k;
            IF @k IS NULL BREAK;
            EXEC dbo.aces_season_refresh @season_key = @k;
            SET @done = @done + 1;
        END;
        IF @done = 0
            PRINT N'No season is running or warming up today. Nothing to refresh.';
        RETURN;
    END;

    DECLARE @d0 date, @d1 date;
    SELECT @d0 = MIN(target_date), @d1 = MAX(target_date)
      FROM dbo.aces_season_forecast WHERE season_key = @season_key;
    IF @d0 IS NULL
        THROW 50001, 'No season rows for that season_key.', 1;

    WITH ac AS (
        SELECT x.d, COUNT(*) AS n
          FROM dbo.aces_v_forecast_inbound_call AS c
         CROSS APPLY (SELECT TRY_CAST(c.event_date AS date) AS d) AS x
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

    -- Days that count: warm-up days (the level) and season days from the 4th on (the start of a
    -- wave can be a day early or late). Skipped: days that look partly loaded (under 40% of
    -- expected), and warm-up days over 1.6 times expected (an event day, such as the business
    -- personal property deadline, would skew the level).
    DECLARE @nc int, @sa float, @se float, @nv int, @va float, @ve float;
    SELECT @nc = COUNT(*), @sa = SUM(CAST(actual_calls AS float)), @se = SUM(calls)
      FROM dbo.aces_season_forecast
     WHERE season_key = @season_key AND (show_in_forecast = 0 OR season_day >= 3)
       AND actual_calls IS NOT NULL AND actual_calls >= 0.4 * calls
       AND (show_in_forecast = 1 OR actual_calls <= 1.6 * calls);
    SELECT @nv = COUNT(*), @va = SUM(CAST(actual_visitors AS float)), @ve = SUM(visitors)
      FROM dbo.aces_season_forecast
     WHERE season_key = @season_key AND (show_in_forecast = 0 OR season_day >= 3)
       AND actual_visitors IS NOT NULL AND actual_visitors >= 0.4 * visitors
       AND (show_in_forecast = 1 OR actual_visitors <= 1.6 * visitors);

    DECLARE @adj_c float = 1, @adj_v float = 1;
    IF @nc > 0 SET @adj_c = 1 + (@sa / @se - 1) * @nc / (@nc + 2.0);
    IF @nv > 0 SET @adj_v = 1 + (@va / @ve - 1) * @nv / (@nv + 2.0);
    SET @adj_c = CASE WHEN @adj_c < 0.6 THEN 0.6 WHEN @adj_c > 1.6 THEN 1.6 ELSE @adj_c END;
    SET @adj_v = CASE WHEN @adj_v < 0.6 THEN 0.6 WHEN @adj_v > 1.6 THEN 1.6 ELSE @adj_v END;

    UPDATE dbo.aces_season_status
       SET adj_calls = @adj_c, adj_visitors = @adj_v, days_calls = @nc, days_visitors = @nv,
           refreshed_utc = SYSUTCDATETIME()
     WHERE season_key = @season_key;
    IF @@ROWCOUNT = 0
        INSERT dbo.aces_season_status (season_key, adj_calls, adj_visitors, days_calls, days_visitors, refreshed_utc)
        VALUES (@season_key, @adj_c, @adj_v, @nc, @nv, SYSUTCDATETIME());

    -- Readout: finished days so far, then the adjustment now in use.
    SELECT @season_key AS season_key, target_date, season_day,
           CAST(ROUND(calls, 0) AS int) AS expected_calls, actual_calls,
           CAST(100.0 * actual_calls / NULLIF(calls, 0) AS decimal(6, 1)) AS calls_pct_of_expected,
           CASE WHEN show_in_forecast = 0 AND actual_calls > 1.6 * calls THEN 'warm-up: skipped, unusually busy'
                WHEN show_in_forecast = 0 AND actual_calls >= 0.4 * calls THEN 'warm-up: sets the level'
                WHEN show_in_forecast = 0 THEN 'warm-up: skipped, looks partly loaded'
                WHEN season_day < 3 THEN 'shown only (start of the wave)'
                WHEN actual_calls < 0.4 * calls THEN 'skipped: looks partly loaded'
                ELSE 'used' END AS calls_status,
           CAST(ROUND(visitors, 0) AS int) AS expected_visitors, actual_visitors,
           CAST(100.0 * actual_visitors / NULLIF(visitors, 0) AS decimal(6, 1)) AS visits_pct_of_expected
      FROM dbo.aces_season_forecast
     WHERE season_key = @season_key AND target_date < @today
     ORDER BY target_date;

    SELECT season_key, CAST(adj_calls AS decimal(5, 2)) AS adj_calls, days_calls,
           CAST(adj_visitors AS decimal(5, 2)) AS adj_visitors, days_visitors, refreshed_utc
      FROM dbo.aces_season_status WHERE season_key = @season_key;
END;
GO
IF USER_ID(N'ASR_CS_User') IS NOT NULL
    GRANT EXECUTE ON dbo.aces_season_refresh TO ASR_CS_User;
GO

/* ---------------------------------------------------------------- 5. ai_api.season_outlook */
CREATE OR ALTER PROCEDURE ai_api.season_outlook
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
IF USER_ID(N'ASR_CS_User') IS NOT NULL
    GRANT EXECUTE ON ai_api.season_outlook TO ASR_CS_User;
GO

/* ---------------------------------------------------------------- 6. ai_api.forecast */
IF OBJECT_ID(N'dbo.aces_season', N'U') IS NULL OR COL_LENGTH(N'dbo.aces_season_forecast', N'show_in_forecast') IS NULL
   OR OBJECT_ID(N'dbo.aces_season_status', N'U') IS NULL OR OBJECT_ID(N'dbo.aces_fn_erlang_agents') IS NULL
    THROW 50001, 'The season tables are missing (see the first error above). ai_api.forecast was not changed.', 1;

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

DECLARE @c95 nvarchar(max), @csl nvarchar(max), @join bit = 0, @path nvarchar(max) = N'';
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
-- 53_TAX_SEASON_FORECAST.sql: season_ columns from dbo.aces_season_forecast, scaled by
--   dbo.aces_season_status (NULL on other dates).
-- 54_SEASON_BUILDER.sql: warm-up days hidden; season_name and season_basis added last.
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
END';

EXEC sys.sp_executesql @sql;
PRINT N'ai_api.forecast updated (54). ' + @path;
GO

/* ---------------------------------------------------------------- 7. build 2027 and check */
EXEC dbo.aces_season_build
     @season_key   = N'NOV_2027',
     @season_type  = N'NOV',
     @mailing_date = '2027-04-01',
     @base_calls   = 139.7,
     @base_visits  = 64.6,
     @season_name  = N'Notice of Value season 2027';
GO
EXEC ai_api.season_outlook @start_date = '2027-04-01', @end_date = '2027-04-09';
EXEC ai_api.forecast @start_date = '2026-11-02', @end_date = '2026-11-04';
