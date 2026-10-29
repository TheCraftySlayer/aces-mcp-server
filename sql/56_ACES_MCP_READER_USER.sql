/*
  56_ACES_MCP_READER_USER.sql

  Database-level half of the A.C.E.S. MCP server's login (run 55 first). Creates the
  database user and grants EXECUTE on the ai_api schema, nothing else, then checks that
  this is all it can do.

  What EXECUTE on ai_api allows: running the ai_api procedures. The procedures read dbo
  tables and write ai_api.tool_log through ownership chaining, which works only while the
  ai_api and dbo schemas have the same owner (check 4 below). The user gets no SELECT,
  INSERT, UPDATE or DELETE on any table or view, and no database roles.

  Run in ASRCustomerService as ASR_CS_Owner (or another db_owner), in SSMS with
  Query > SQLCMD Mode on, or with sqlcmd -i. The checks use EXECUTE AS USER, which needs
  IMPERSONATE (db_owner has it). Safe to rerun.

  For a Windows login (option B in 55), set ReaderLogin to DOMAIN\account here too.

  Then point the server at it: ACES_SQL_USER=ACES_MCP_Reader with ACES_SQL_AUTH=SqlPassword,
  or ACES_SQL_AUTH=Windows for a Windows login. On a PC, change $onPremSqlUser in
  start-aces.ps1 and run "start-aces.bat reset" to enter the new password.
*/
:on error exit
:setvar ReaderLogin "ACES_MCP_Reader"
:setvar Database "ASRCustomerService"

USE [$(Database)];
GO

IF USER_ID(N'$(ReaderLogin)') IS NULL
BEGIN
    CREATE USER [$(ReaderLogin)] FOR LOGIN [$(ReaderLogin)] WITH DEFAULT_SCHEMA = ai_api;
    PRINT N'Created user $(ReaderLogin).';
END
GO

GRANT EXECUTE ON SCHEMA::ai_api TO [$(ReaderLogin)];
GO

:on error ignore
/* ------------------------------------------------------------------- Checks ---------- */

/* 1. Database roles. Expect no rows. */
SELECT r.name AS database_role
  FROM sys.database_role_members m
  JOIN sys.database_principals r ON r.principal_id = m.role_principal_id
  JOIN sys.database_principals u ON u.principal_id = m.member_principal_id
 WHERE u.name = N'$(ReaderLogin)';

/* 2. Permissions granted to the user. Expect exactly two rows:
      DATABASE  CONNECT  GRANT
      SCHEMA    EXECUTE  GRANT  ai_api                                                  */
SELECT p.class_desc, p.permission_name, p.state_desc,
       CASE p.class_desc
            WHEN N'SCHEMA' THEN SCHEMA_NAME(p.major_id)
            WHEN N'OBJECT_OR_COLUMN' THEN OBJECT_SCHEMA_NAME(p.major_id) + N'.' + OBJECT_NAME(p.major_id)
            ELSE N''
       END AS securable
  FROM sys.database_permissions p
  JOIN sys.database_principals u ON u.principal_id = p.grantee_principal_id
 WHERE u.name = N'$(ReaderLogin)'
 ORDER BY p.class_desc, p.permission_name;

/* 3. Every user also gets what the public role has. Expect no rows: nothing on tables or
      views should be granted to public.                                               */
SELECT OBJECT_SCHEMA_NAME(p.major_id) + N'.' + OBJECT_NAME(p.major_id) AS granted_to_public,
       p.permission_name
  FROM sys.database_permissions p
 WHERE p.grantee_principal_id = DATABASE_PRINCIPAL_ID(N'public')
   AND p.class_desc = N'OBJECT_OR_COLUMN'
   AND OBJECTPROPERTY(p.major_id, 'IsMSShipped') = 0
   AND (OBJECTPROPERTY(p.major_id, 'IsUserTable') = 1 OR OBJECTPROPERTY(p.major_id, 'IsView') = 1);

/* 4. Schema owners. ai_api and dbo must show the same owner (normally dbo), or the
      procedures can't read dbo tables for this user.                                  */
SELECT s.name AS schema_name, USER_NAME(s.principal_id) AS owner
  FROM sys.schemas s
 WHERE s.name IN (N'ai_api', N'dbo');

/* 5. What the user can actually do, checked as the user.                               */
DECLARE @someTable nvarchar(300) =
    (SELECT TOP (1) QUOTENAME(SCHEMA_NAME(t.schema_id)) + N'.' + QUOTENAME(t.name)
       FROM sys.tables t WHERE SCHEMA_NAME(t.schema_id) = N'dbo' AND t.is_ms_shipped = 0 ORDER BY t.name);

EXECUTE AS USER = N'$(ReaderLogin)';

SELECT HAS_PERMS_BY_NAME(N'ai_api', N'SCHEMA', N'EXECUTE') AS can_execute_ai_api,  -- expect 1
       HAS_PERMS_BY_NAME(N'dbo', N'SCHEMA', N'SELECT')     AS can_select_dbo,       -- expect 0
       HAS_PERMS_BY_NAME(N'dbo', N'SCHEMA', N'INSERT')     AS can_insert_dbo,       -- expect 0
       HAS_PERMS_BY_NAME(N'dbo', N'SCHEMA', N'UPDATE')     AS can_update_dbo,       -- expect 0
       HAS_PERMS_BY_NAME(N'dbo', N'SCHEMA', N'DELETE')     AS can_delete_dbo,       -- expect 0
       HAS_PERMS_BY_NAME(N'ai_api', N'SCHEMA', N'SELECT')  AS can_select_ai_api;    -- expect 0

BEGIN TRY
    EXEC ai_api.deed_freshness;
    PRINT N'ok: the user can run an ai_api procedure (ai_api.deed_freshness).';
END TRY
BEGIN CATCH
    PRINT N'PROBLEM running ai_api.deed_freshness as the user: ' + ERROR_MESSAGE();
END CATCH;

IF @someTable IS NOT NULL
BEGIN
    BEGIN TRY
        EXEC (N'SELECT TOP (0) * FROM ' + @someTable);
        PRINT N'PROBLEM: the user can read ' + @someTable + N' directly.';
    END TRY
    BEGIN CATCH
        IF ERROR_NUMBER() = 229
            PRINT N'ok: reading ' + @someTable + N' directly is refused.';
        ELSE
            PRINT N'check by hand: ' + ERROR_MESSAGE();
    END CATCH;
END;

REVERT;
GO
