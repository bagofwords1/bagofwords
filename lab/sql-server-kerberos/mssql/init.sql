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
-- AD logins (Windows authentication). Kerberos-authenticated connections for
-- these principals will succeed and report auth_scheme = KERBEROS.
IF SUSER_ID('BOWLAB\alice') IS NULL
    CREATE LOGIN [BOWLAB\alice] FROM WINDOWS WITH DEFAULT_DATABASE = bowlab;
GO
IF SUSER_ID('BOWLAB\bob') IS NULL
    CREATE LOGIN [BOWLAB\bob] FROM WINDOWS WITH DEFAULT_DATABASE = bowlab;
GO
IF SUSER_ID('BOWLAB\svc-bow') IS NULL
    CREATE LOGIN [BOWLAB\svc-bow] FROM WINDOWS WITH DEFAULT_DATABASE = bowlab;
GO
USE bowlab;
GO
IF USER_ID('BOWLAB\alice') IS NULL
    CREATE USER [BOWLAB\alice] FOR LOGIN [BOWLAB\alice];
GO
IF USER_ID('BOWLAB\bob') IS NULL
    CREATE USER [BOWLAB\bob] FOR LOGIN [BOWLAB\bob];
GO
IF USER_ID('BOWLAB\svc-bow') IS NULL
    CREATE USER [BOWLAB\svc-bow] FOR LOGIN [BOWLAB\svc-bow];
GO
-- Alice and the service account can read; bob deliberately cannot.
IF NOT EXISTS (
    SELECT 1 FROM sys.database_role_members rm
    JOIN sys.database_principals role_p ON role_p.principal_id = rm.role_principal_id
    JOIN sys.database_principals member_p ON member_p.principal_id = rm.member_principal_id
    WHERE role_p.name = 'db_datareader' AND member_p.name = 'BOWLAB\alice'
)
    ALTER ROLE db_datareader ADD MEMBER [BOWLAB\alice];
GO
IF NOT EXISTS (
    SELECT 1 FROM sys.database_role_members rm
    JOIN sys.database_principals role_p ON role_p.principal_id = rm.role_principal_id
    JOIN sys.database_principals member_p ON member_p.principal_id = rm.member_principal_id
    WHERE role_p.name = 'db_datareader' AND member_p.name = 'BOWLAB\svc-bow'
)
    ALTER ROLE db_datareader ADD MEMBER [BOWLAB\svc-bow];
GO
-- Let the SQL Server 2022 lab assertions read auth_scheme for their own sessions.
USE master;
GO
GRANT VIEW SERVER PERFORMANCE STATE TO [BOWLAB\alice];
GO
GRANT VIEW SERVER PERFORMANCE STATE TO [BOWLAB\svc-bow];
GO
