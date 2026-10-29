/* ---------------------------------------------------------------------------
   56: a database login for the A.C.E.S. server that can only run ai_api procedures.

   For the county server. The server runs as a Windows service account and signs in to
   SQL Server as that account (ACES_SQL_AUTH=Windows), so no password is stored anywhere.
   The account gets one role, ai_api_executor, which can EXECUTE the ai_api procedures and
   nothing else: no reading or changing tables directly. That replaces ASR_CS_User for
   the server, so read-only no longer rests on the server's code alone.

   Run in SSMS on asbcdbbernt as someone who can create logins (a DBA).
   1. Set @account on the first line below to the service account, as DOMAIN\name.
      A group managed service account ends in $.
   2. Run the whole script. It's safe to run again.
   3. Read the results: one schema owner, one role, a deed freshness row, then 0 and 0.
   Then rerun sql/export_ai_api.py and commit sql/current/ai_api.sql.

   To undo, in ASRCustomerService: DROP USER [<account>]; then DROP LOGIN [<account>];
   --------------------------------------------------------------------------- */

DECLARE @account sysname = N'DOMAIN\svc-aces$';   -- <-- the service account

DECLARE @sql nvarchar(max);

IF @account = N'DOMAIN\svc-aces$'
BEGIN
    RAISERROR(N'Set @account to the real service account first. Nothing was changed.', 16, 1);
    RETURN;
END

/* Server login from the Windows account (no password). */
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = @account)
BEGIN
    SET @sql = N'CREATE LOGIN ' + QUOTENAME(@account) + N' FROM WINDOWS WITH DEFAULT_DATABASE = [ASRCustomerService];';
    EXEC (@sql);
    PRINT N'Created login ' + @account;
END

/* Database user, in ai_api_executor only. Never db_datareader or db_datawriter. */
SET @sql = N'
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = @account)
BEGIN
    DECLARE @create nvarchar(max) = N''CREATE USER '' + QUOTENAME(@account) + N'' FOR LOGIN '' + QUOTENAME(@account)
        + N'' WITH DEFAULT_SCHEMA = [ai_api];'';
    EXEC (@create);
    PRINT N''Created database user '' + @account;
END
IF IS_ROLEMEMBER(N''ai_api_executor'', @account) = 0
BEGIN
    DECLARE @add nvarchar(max) = N''ALTER ROLE [ai_api_executor] ADD MEMBER '' + QUOTENAME(@account) + N'';'';
    EXEC (@add);
    PRINT N''Added '' + @account + N'' to ai_api_executor'';
END';
EXEC [ASRCustomerService].sys.sp_executesql @sql, N'@account sysname', @account = @account;

/* The procedures read ai_read and dbo tables and write ai_api.tool_log. A login with only
   EXECUTE can do that through ownership chaining, which needs every schema they touch to
   have the same owner. This should show one owner for all three. If it shows more than
   one, stop: the test below will fail, and the schema owners need to sort it out first. */
SELECT s.name AS schema_name, p.name AS schema_owner
FROM [ASRCustomerService].sys.schemas AS s
JOIN [ASRCustomerService].sys.database_principals AS p ON p.principal_id = s.principal_id
WHERE s.name IN (N'ai_api', N'ai_read', N'dbo');

/* What the account can do: this should list ai_api_executor and nothing else. */
SELECT r.name AS role_name
FROM [ASRCustomerService].sys.database_role_members AS m
JOIN [ASRCustomerService].sys.database_principals AS r ON r.principal_id = m.role_principal_id
JOIN [ASRCustomerService].sys.database_principals AS u ON u.principal_id = m.member_principal_id
WHERE u.name = @account;

/* Test as the account: one cheap procedure call (it logs one row to ai_api.tool_log),
   then proof it can't read or change a table directly. Expect a row, then 0 and 0. */
SET @sql = N'
EXECUTE AS LOGIN = @account;
EXEC ai_api.deed_freshness;
SELECT HAS_PERMS_BY_NAME(N''ai_api.tool_log'', N''OBJECT'', N''SELECT'') AS can_read_tool_log_directly,
       HAS_PERMS_BY_NAME(N''dbo.raw_calendar'', N''OBJECT'', N''UPDATE'') AS can_change_a_dbo_table;
REVERT;';
EXEC [ASRCustomerService].sys.sp_executesql @sql, N'@account sysname', @account = @account;
