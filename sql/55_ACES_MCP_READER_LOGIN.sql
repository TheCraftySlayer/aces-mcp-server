/*
  55_ACES_MCP_READER_LOGIN.sql

  Server-level half of the A.C.E.S. MCP server's database login. Run this first, then
  56_ACES_MCP_READER_USER.sql in the database.

  Why: on the county server, the A.C.E.S. MCP server has been signing in as ASR_CS_User,
  which can read and change every table (and, per 53 and 54, run dbo.aces_season_refresh).
  The server only ever calls ai_api procedures, so its login should be able to do nothing
  else. That's already true on Azure, where the web app's managed identity has EXECUTE on
  ai_api and nothing more. This brings the county server to the same place.

  Who runs it: someone with ALTER ANY LOGIN on the instance (the DBA), either
    - in SSMS with Query > SQLCMD Mode on: uncomment the ":setvar ReaderPassword" line
      below and type the password for this run only. Don't save the file with it.
    - or with sqlcmd: sqlcmd -S asbcdbbernt -E -i 55_ACES_MCP_READER_LOGIN.sql -v ReaderPassword="..."
      (the password then sits in that shell's history, so SSMS is the better choice).
  The password needs 16 or more characters and no single quote.

  Pick ONE option:
    A. SQL login (default below). The server uses ACES_SQL_AUTH=SqlPassword with
       ACES_SQL_USER=ACES_MCP_Reader and the password in its secrets.
    B. Windows login for the service account the server runs under on the County AI Lab
       server. Nothing secret is stored; the server uses ACES_SQL_AUTH=Windows. Comment out
       option A, uncomment option B, and set ReaderLogin to DOMAIN\account in both scripts.

  Safe to rerun (with the password set): an existing login is left as it is.
*/
:on error exit
:setvar ReaderLogin "ACES_MCP_Reader"
:setvar Database "ASRCustomerService"
-- :setvar ReaderPassword ""

USE master;
GO

/* ---- Option A: SQL login ------------------------------------------------------------ */
IF SUSER_ID(N'$(ReaderLogin)') IS NULL
BEGIN
    IF LEN(N'$(ReaderPassword)') < 16
        THROW 50000, N'ReaderPassword needs 16 or more characters.', 1;

    -- CHECK_POLICY applies the Windows password rules. Expiration is off because this is a
    -- service login; rotate the password on the office's schedule instead.
    CREATE LOGIN [$(ReaderLogin)]
        WITH PASSWORD = N'$(ReaderPassword)',
             DEFAULT_DATABASE = [$(Database)],
             CHECK_POLICY = ON,
             CHECK_EXPIRATION = OFF;
    PRINT N'Created login $(ReaderLogin).';
END
ELSE
    PRINT N'Login $(ReaderLogin) already exists; left as it is.';
GO

/* ---- Option B: Windows login (service account) -------------------------------------- */
-- IF SUSER_ID(N'$(ReaderLogin)') IS NULL
--     CREATE LOGIN [$(ReaderLogin)] FROM WINDOWS WITH DEFAULT_DATABASE = [$(Database)];
-- GO

/* The login must hold no server roles. Expect no rows. */
SELECT r.name AS server_role
  FROM sys.server_role_members m
  JOIN sys.server_principals r ON r.principal_id = m.role_principal_id
  JOIN sys.server_principals l ON l.principal_id = m.member_principal_id
 WHERE l.name = N'$(ReaderLogin)';
GO
