/*
  55_ASSESSMENT_CALENDAR_LOGGING.sql

  Makes ai_api.assessment_calendar log its calls, like the other nine ai_api procedures.

  Why: the ai_api snapshot (sql/current/ai_api.sql) showed this procedure was deployed
  with its template note still in place ("TOOL_LOG: paste the logging INSERT from
  ai_api.office_volume here"), so its calls were never written to ai_api.tool_log. That
  includes the calendar lookup inside every ACES_Daily_Briefing. The README says every
  call is logged.

  The change: the placeholder becomes the same INSERT the other procedures use, with this
  procedure's three parameters, placed after the input checks and after @as_of is filled
  in, so the log shows the date actually used. Nothing else in the procedure changes; the
  body below is copied from the snapshot exported 2026-10-29 17:10 UTC.

  Run on: ASRCustomerService on asbcdbbernt, as ASR_CS_Owner, in SSMS (no SQLCMD mode
  needed). On any other copy of the database, export that copy first and check its
  assessment_calendar matches before running this.

  Safe to rerun. CREATE OR ALTER keeps the existing permissions.
  Afterward: rerun sql/export_ai_api.py and commit the new snapshot.
*/
GO

/* ---------------------------------------------------------------------------
   @as_of comes from the server in Mountain time. If it's NULL, the procedure
   works out Mountain "today" itself, because Azure SQL's clock is UTC.

   Returns events that overlap the window, plus any older event whose expected
   contact-impact window covers today (so a mailing from five weeks ago still
   shows up while it's driving calls).

   Logs each call to ai_api.tool_log (55_ASSESSMENT_CALENDAR_LOGGING.sql).
   --------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE ai_api.assessment_calendar
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

/* Check: one call should add one tool_log row for assessment_calendar.
   Expect one row, with params like: as_of=2026-10-29 days_ahead=1 days_back=0 */
DECLARE @before bigint = ISNULL((SELECT MAX(log_id) FROM ai_api.tool_log), 0);
EXEC ai_api.assessment_calendar @days_ahead = 1, @days_back = 0;
SELECT log_id, called_utc, proc_name, params, caller
  FROM ai_api.tool_log
 WHERE log_id > @before
   AND proc_name = N'assessment_calendar';
GO
