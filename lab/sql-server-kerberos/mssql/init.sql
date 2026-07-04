-- Lab database + AD-backed logins for the Kerberos delegation test.
-- The AD users are impersonated by the app via S4U2Proxy; SQL Server sees
-- their real domain identity, so per-user logins/permissions apply.
IF DB_ID('bowlab') IS NULL
    CREATE DATABASE bowlab;
GO
USE bowlab;
GO
IF OBJECT_ID('dbo.sales') IS NULL
BEGIN
    CREATE TABLE dbo.sales (id INT PRIMARY KEY, region NVARCHAR(50), amount DECIMAL(10,2));
    INSERT INTO dbo.sales VALUES (1, N'North', 100.00), (2, N'South', 250.50), (3, N'East', 75.25);
END
GO
-- A second table only bob may read, so alice's and bob's visible catalogs are
-- disjoint — proving the per-user overlay is genuinely per-identity, not a
-- subset of one shared catalog.
IF OBJECT_ID('dbo.audit') IS NULL
BEGIN
    CREATE TABLE dbo.audit (id INT PRIMARY KEY, event NVARCHAR(100));
    INSERT INTO dbo.audit VALUES (1, N'login'), (2, N'export');
END
GO
-- AD logins (Windows authentication). Kerberos-authenticated connections for
-- these principals will succeed and report auth_scheme = KERBEROS.
IF SUSER_ID('BOWLAB\alice') IS NULL
    CREATE LOGIN [BOWLAB\alice] FROM WINDOWS WITH DEFAULT_DATABASE = bowlab;
GO
IF SUSER_ID('BOWLAB\bob') IS NULL
    CREATE LOGIN [BOWLAB\bob] FROM WINDOWS WITH DEFAULT_DATABASE = bowlab;
GO
USE bowlab;
GO
IF USER_ID('BOWLAB\alice') IS NULL
    CREATE USER [BOWLAB\alice] FOR LOGIN [BOWLAB\alice];
GO
IF USER_ID('BOWLAB\bob') IS NULL
    CREATE USER [BOWLAB\bob] FOR LOGIN [BOWLAB\bob];
GO
-- Table-specific grants (NOT db_datareader) so each user sees only their own
-- tables in INFORMATION_SCHEMA: alice → dbo.sales, bob → dbo.audit. This makes
-- their introspected overlays disjoint, and bob is still denied on dbo.sales.
GRANT SELECT ON dbo.sales TO [BOWLAB\alice];
GO
GRANT SELECT ON dbo.audit TO [BOWLAB\bob];
GO
-- Let the test read auth_scheme from sys.dm_exec_connections. The required
-- permission was renamed in SQL Server 2022; grant both and ignore the one that
-- doesn't exist on a given version (each batch is independent).
BEGIN TRY GRANT VIEW SERVER STATE TO [BOWLAB\alice]; END TRY BEGIN CATCH END CATCH;
GO
BEGIN TRY GRANT VIEW SERVER PERFORMANCE STATE TO [BOWLAB\alice]; END TRY BEGIN CATCH END CATCH;
GO
