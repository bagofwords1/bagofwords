# Linux/Python clients for running DAX against on-prem SSAS / PBIRS Tabular, and the authentication options

Scope: on-premises SQL Server Analysis Services (2016+ Tabular) and the Analysis Services engine embedded in Power BI Report Server (PBIRS). Cloud targets (Azure AS, Power BI/Fabric XMLA endpoints) are mentioned only to rule them out. Research date: 2026-10-23. Tool-call budget was exceeded (~40 calls) because several Microsoft Learn rowset pages and community.fabric.microsoft.com threads returned 404/403; gaps are flagged below.

---

## KQ1. Which client-side approaches actually work from Linux against on-prem SSAS with Windows (NTLM/Kerberos) auth, and what do real users report?

### Takeaway
The only approach Microsoft documents for non-Windows clients is XMLA/SOAP over HTTP(S) through the IIS `msmdpump.dll` pump, where IIS does the Windows authentication (Anonymous, Basic, or Windows Integrated/NTLM/Kerberos) and the pump talks TCP to SSAS as a Windows identity. Direct TCP (port 2383) from Linux is not possible with Microsoft libraries (ADOMD.NET Core explicitly throws on non-Windows for TCP) and no open-source Linux implementation of the proprietary SSAS TCP protocol was found. Commercial ODBC/JDBC drivers (CData) for Linux are also just HTTP/XMLA clients against `msmdpump.dll`.

### Cited Findings

**Microsoft's own positioning of the HTTP pump as the non-Windows path**
- Microsoft lists as a reason to configure the pump: "Client applications can't use the Analysis Services client libraries (for example, a Java application running on a UNIX server). If you can't use the Analysis Services client libraries for data access, you can use SOAP and XML/A over a direct HTTP connection to an Analysis Services instance." — [Configure HTTP Access to Analysis Services on IIS 8.0](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- "Authentication methods other than Windows integrated security are required. Specifically, you can use Anonymous connections and Basic authentication when configuring Analysis Services for HTTP access. Digest, Forms, and ASP.NET authentication are not supported." — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- "SQL Server Analysis Services uses Windows authentication only. Basic authentication and anonymous authentication can be used if HTTP access is enabled through MSMDPUMP.dll" — [Connection string properties (Analysis Services)](https://learn.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services)
- "Connections from a client application to an Analysis Services instance require Windows authentication (integrated). You can provide a Windows user identity using any of the following methods: NTLM; Kerberos; EffectiveUserName on the connection string; Basic or Anonymous (requires configuration for HTTP access); Stored credentials ... Notice that Claims authentication is not supported. You cannot use a Windows Claims token to access Analysis Services. The Analysis Services client libraries only work with Windows security principles." — [Authentication methodologies supported by Analysis Services (SQL 2012 archive)](https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2012/dn141154(v=sql.110))
- "Basic and Anonymous are available only when you configure Analysis Services for HTTP access, using IIS and the msmdpump.dll to establish the connection." — [same](https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2012/dn141154(v=sql.110))

**Direct TCP from Linux: Microsoft ADOMD.NET Core refuses**
- "Supported scenarios include connections to Azure Analysis Services, Power BI Premium, and SQL Server Analysis Services. TCP based connectivity is supported for Windows computers only." — [Analysis Services client libraries, "AMO and ADOMD .Net Core"](https://learn.microsoft.com/en-us/analysis-services/client-libraries)
- Real-user report (Jan 2021): .NET Core 3.1 app with `Microsoft.AnalysisServices.AdomdClient` NetCore, connection string `Data Sources={server};Initial Catalog={catalog};UID={Username};PWD={password}` works on Windows; on Linux: `System.NotSupportedException: This feature is supported for a .NET Core client only on Windows systems. at Microsoft.AnalysisServices.AdomdClient.XmlaClient.OpenConnectionAndCheckIfSessionTokenNeeded(ConnectionInfo connectionInfo)`. Darren Gosbell (MVP, DAX Studio author): "when you are connecting to an on-prem SSAS server using <servername>\<instancename> it requires Windows authentication - which is not available on Linux. If you are connecting to Azure AS or Power BI Premium XMLA endpoints it uses OAuth2 which is supported on linux. You could maybe try configuring HTTP Access on your server and connecting via that." and later "should work as long as you enable basic auth". Microsoft staff (Lukas Yu) gave no definitive answer and pointed to a CData ODBC-on-Linux article. — [Microsoft Q&A: Can adomd .net core connect to an SSAS endpoint from Linux?](https://learn.microsoft.com/en-us/answers/questions/233745/can-adomd-net-core-connect-to-an-ssas-endpoint-fro)

**No open-source Linux implementation of the SSAS TCP protocol found**
- A search for open-source clients of the TCP 2383 protocol returned only XMLA-over-HTTP material and a Windows-only pythonnet/ADOMD MCP server; no MS-SSAS wire-protocol implementation surfaced. — [search summary; see e.g. SSAS MCP Server listing](https://glama.ai/mcp/servers/afnmcu4564)
- The `olap.xmla` GitHub repo (alloytech/olap) "is archived and read-only"; `python_xmla` (kthejoker) relies on Analysis Services DLLs from a SQL Server 2019 install plus `dotnet-runtime` installed via `apt-get` on Databricks, i.e. it is the ADOMD-on-.NET-Core route, not a native protocol client. — [alloytech/olap](https://github.com/alloytech/olap); [kthejoker/python_xmla](https://github.com/kthejoker/python_xmla)

**Databricks (Linux) user attempt**
- A Databricks user trying `pyodbc` with "ODBC Driver 17 for SQL Server" got `[HYT00] ... Login timeout expired`; Databricks employee reply: "pyodbc cannot be used directly to connect to SSAS models or Power BI XMLA endpoints", recommended pyadomd (untested) and said MSOLAP+adodbapi "has more compatibility issues on Databricks". No working on-prem solution was confirmed in the thread. — [Databricks Community thread](https://community.databricks.com/t5/data-engineering/connect-to-sql-server-analysis-services-ssas-server-to-run-dax/td-p/111142)

**Commercial Linux drivers go through the pump too**
- CData's Linux ODBC guide: connect to SSAS "instances hosted over HTTP with XMLA access"; `URL` example `http://localhost/OLAP/msmdpump.dll`; AuthScheme `Basic`/`Digest`, `NTLM` ("Set the Windows User and Password and set AuthScheme to 'NTLM'"), `NEGOTIATE` for Kerberos, `KERBEROSDELEGATION` for delegation. — [CData: SSAS ODBC + Python on Linux](https://www.cdata.com/kb/tech/ssas-odbc-python-linux.rst)

### Inferences
- For a Python/Linux backend the realistic design is: Python -> HTTPS -> IIS (`/OLAP/msmdpump.dll`) -> TCP -> SSAS. Everything else (pyadomd/pythonnet + ADOMD NetCore, CData ODBC/JDBC) either fails on Linux for on-prem TCP or is itself an HTTP/XMLA client to the same pump, adding a license or a .NET runtime without removing the IIS dependency.
- Because the pump is the only documented path, the IIS auth mode you pick (Basic over TLS vs Windows Integrated) determines which Python auth library you need (plain `requests` auth vs `requests-ntlm`/`requests-kerberos`).

### Gaps
- No published, end-to-end example of Python `requests` + `requests-ntlm`/`pyspnego` posting DAX SOAP `Execute` to `msmdpump.dll` was found; the combination is supported by the pieces (IIS Windows auth = NTLM/Kerberos; requests-ntlm/requests-kerberos implement those) but no first-hand report confirms it. Needs lab verification.
- Reddit/Stack Overflow first-hand Linux reports were not reachable within the budget.

---

## KQ2. Does ADOMD.NET (.NET Core / .NET 6+) on Linux support on-prem SSAS with Windows credentials, or only Entra ID targets?

### Takeaway
ADOMD.NET (.NET Core/.NET 6/8 builds) supports on-prem SSAS only over TCP on Windows; on Linux it throws `NotSupportedException` for the TCP/Windows-auth path. Entra ID (OAuth) targets work cross-platform (non-interactive), and HTTP-pump connections with Basic credentials are the only on-prem possibility that is not explicitly excluded, but it is not documented as supported on Linux.

### Cited Findings
- "Starting Feb. 2021, .NET Core runtime support is available for the AMO and ADOMD client packages. There are, however, a few scenarios not-supported by the .NET Core versions." — [Analysis Services client libraries](https://learn.microsoft.com/en-us/analysis-services/client-libraries)
- "AMO and ADOMD .Net Core — Supported scenarios include connections to Azure Analysis Services, Power BI Premium, and SQL Server Analysis Services. TCP based connectivity is supported for Windows computers only. Interactive login with Microsoft Entra ID is supported for Windows computers only. The .NET Core Desktop runtime is required." — [same](https://learn.microsoft.com/en-us/analysis-services/client-libraries)
- "Version 19.12.7.2 of the .Net Core client libraries introduced support for SQL Server Analysis Services. Lower preview versions only supported Azure Analysis Services and Power BI semantic models." — [same](https://learn.microsoft.com/en-us/analysis-services/client-libraries)
- "Starting July 2024, the AMO and ADOMD packages contain multi-runtime releases for all the supported target runtimes, both .NET FX as well as .NET Core ... The new multi-runtime packages no longer have the '.retail.amd64' suffix in the package identity and are now simply called Microsoft.AnalysisServices and Microsoft.AnalysisServices.AdomdClient. The older ... '.NetCore.retail.amd64' suffix for .NET Core" packages remain for backward compatibility. Current version 19.118.3.0. "Starting with version 19.84.6 ... AS client-side assemblies are now released with support for .NET FX 4.7.2, as well as .NET 6.0 and .NET 8.0 only." — [same](https://learn.microsoft.com/en-us/analysis-services/client-libraries)
- The runtime exception text on Linux for on-prem TCP: "System.NotSupportedException: This feature is supported for a .NET Core client only on Windows systems." (stack: `XmlaClient.OpenConnectionAndCheckIfSessionTokenNeeded` -> `AdomdConnection.ConnectToXMLA(Boolean createSession, Boolean isHTTP)`). — [Microsoft Q&A thread](https://learn.microsoft.com/en-us/answers/questions/233745/can-adomd-net-core-connect-to-an-ssas-endpoint-fro)
- User/Password semantics by transport: "When connecting over TCP to SSAS, the client library will impersonate the Windows user using the specified username and password, and then connect as usual to the server. When connecting over HTTP(S) to SSAS, the credentials are provided to the web server based on the authentication mode configured on the web server, for example Basic auth or Windows auth. The web server will perform the appropriate Windows impersonation before connecting to the SSAS server" — [Connection string properties](https://learn.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services)
- "Beginning with version 19.67.0, connectivity objects like ... AdomdConnection ... support a new AccessToken property that enables an improved way to pass external OAuth tokens" (cloud only). — [client libraries](https://learn.microsoft.com/en-us/analysis-services/client-libraries)
- pyadomd (pythonnet wrapper) status: issue #3 "using NetCore version" (Jan 2021) — maintainer: "the project are using the Pythonnet project for interfacing with .net dll, and that project is not supporting .net core, yet." A June 2022 follow-up ("pythonnet is now supporting .net core ... what's the work involved") has no maintainer reply. — [S-C-O-U-T/Pyadomd issue #3](https://github.com/S-C-O-U-T/Pyadomd/issues/3)
- pyadomd open issues include "#24 No Credentials Available in the Security Package" (Nov 2023), "#27 System.IO.FileNotFoundException" (Mar 2025, 7 comments), "#29 pip install error - Deprecation" (Sep 2025); PyPI badge lists only Windows 10. — [Pyadomd issues](https://github.com/S-C-O-U-T/Pyadomd/issues); [pyadomd on PyPI](https://pypi.org/project/pyadomd)
- A vendor (Zetaris) walkthrough runs pyadomd on .NET 8 via pythonnet `coreclr` with ADOMD assemblies from NuGet — but against Azure Analysis Services with MSAL, not on-prem. — [Zetaris KB](https://kbase.zetaris.com/knowledge/connecting-zetaris-notebook-to-azure-analysis-services-aas)
- Impersonation note: the stack trace shows `Microsoft.AnalysisServices.Platform.Security.TransparentUserContext.ExecuteInUserContextImpl`, i.e. the TCP path impersonates a Windows logon, which has no Linux equivalent. — [Q&A thread](https://learn.microsoft.com/en-us/answers/questions/233745/can-adomd-net-core-connect-to-an-ssas-endpoint-fro)

### Inferences
- A `dotnet` sidecar on Linux (or pythonnet + coreclr) using the official ADOMD package will fail for `Data Source=server\instance` regardless of UID/PWD, because the UID/PWD TCP path requires Windows impersonation (LogonUser) which the library only implements on Windows.
- ADOMD .NET Core over `Data Source=https://host/OLAP/msmdpump.dll;Integrated Security=Basic;User ID=...;Password=...` is not excluded by the documentation (only "TCP based connectivity" and "interactive Entra login" are Windows-only) and the Q&A thread's expert suggests it "should work as long as you enable basic auth" — but nobody in the sources confirmed it on Linux. If the only reason to use ADOMD is response parsing, plain Python SOAP is simpler.

### Gaps
- No Microsoft statement found that explicitly says "HTTP/Basic to msmdpump from ADOMD .NET Core on Linux is supported"; it is an inference from the absence of exclusion.
- No GitHub issue on a Microsoft repo was found (the AS client libraries are closed-source NuGet packages with no public issue tracker).

---

## KQ3. How is msmdpump.dll configured, and does requests-ntlm or pyspnego Negotiate work against it? NTLM keep-alive gotchas?

### Takeaway
The pump is an ISAPI extension copied into an IIS application (`\inetpub\wwwroot\OLAP\`), with `msmdpump.ini` `<ServerName>` pointing at the SSAS instance; IIS auth (Anonymous / Basic / Windows) selects the identity. SSAS 2025's pump requires HTTPS by default (`RequireSecureChannel`). Python can authenticate with `requests` HTTPBasicAuth (Basic), `requests-ntlm` (NTLM via pyspnego) or `requests-kerberos` (Negotiate/Kerberos via pyspnego + system krb5); NTLM is connection-bound, so use a `requests.Session` and expect proxies/load balancers without connection pinning to break it.

### Cited Findings

**Setup steps (Microsoft)**
- "Copy the following files, found at <drive>:\Program Files\Microsoft SQL Server\<instance>\OLAP\bin\isapi: MSMDPUMP.DLL, MSMDPUMP.INI, and a Resources folder" to "<drive>:\inetpub\wwwroot\OLAP". "The drive must be formatted for the NTFS file system. The path to the folder that you create must not contain any spaces." — [Configure HTTP Access](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- IIS features required: "Security | Windows Authentication, or Basic Authentication ... Application Development | CGI ... ISAPI Extensions". — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- App pool: "Create an application pool named OLAP, using the .NET Framework, with Managed pipeline mode set to Classic." "By default, IIS creates application pools using ApplicationPoolIdentity as the security identity, which is a valid choice for HTTP access to Analysis Services." 64-bit: "Enable 32-bit Applications ... false" if the DLL came from 64-bit SSAS. — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- Handler mapping: "Add Script Map ... specify *.dll as the request path, specify c:\inetpub\wwwroot\OLAP\msmdpump.dll as the executable, and type OLAP as the name." — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- `msmdpump.ini` default: `<ConfigurationSettings><ServerName>localhost</ServerName><SessionTimeout>3600</SessionTimeout><ConnectionPoolSize>100</ConnectionPoolSize></ConfigurationSettings>`; "you must specify the server name (for example, <ServerName>ADWRKS-SRV01</ServerName>). For a server that is installed as a named instance, be sure to append the instance name (for example, <ServerName>ADWRKS-SRV01\Tabular</ServerName>)"; fixed port: "<ServerName>AW-SRV01:55555</ServerName>". — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- Remote SSAS: "A remote connection between IIS and Analysis Services requires that you install the Analysis Services OLE DB provider (MSOLAP) on the Windows server running IIS." "On IIS, MSMDPUMP connects to Analysis Services using the Analysis Services OLE DB provider over TCP/IP. Although client requests can originate outside of domain trust, both Analysis Services and IIS must be in the same domain or in trusted domains". — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- "The client libraries required by client applications can't connect to Analysis Services through proxy servers that require a username and password." — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)

**Supported auth modes and semantics**
- "Supported authentication methods for Analysis Services over HTTP include: Windows authentication (Kerberos or NTLM); Basic authentication; Anonymous authentication". — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- Basic: "IIS will try to impersonate the user using the provided credentials when connecting to MSMDPUMP, but the credentials won't be delegated to Analysis Services. Instead, you'll need to pass a valid username and password on a connection ... Because Analysis Services supports only Windows authentication, any credentials passed to it must be a Windows user or group that is a member of the domain in which Analysis Services is hosted." — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- "Disable Anonymous Authentication if you're using Windows or Basic authentication. When Anonymous authentication is enabled, IIS will always use it first, even if other authentication methods are enabled." Under Anonymous "the pump (msmdpump.dll) runs as the user account you established for anonymous user ... By default, IIS uses the IUSR account". — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- Connection string for Basic: `Data Source=https://<servername>/olap/msmdpump.dll; Initial Catalog=AdventureWorksDW2012; Integrated Security=Basic; User ID=XXXX; Password=XXXXX;` — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- "blank is the default value for HTTP connections" for Integrated Security; "SSPI is the default value for TCP connections, allowing NTLM, Kerberos, or Anonymous authentication." — [Connection string properties](https://learn.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services)

**HTTPS requirement**
- "Starting with SQL Server Analysis Services 2025, HTTP connections through msmdpump.dll will be disabled by default." Error: "Connections to SQL Server Analysis Services through msmdpump.dll must use secure channels, for example HTTPS. (Unknown)". Test-only override: `<RequireSecureChannel>false</RequireSecureChannel>` in msmdpump.ini. For earlier versions: "it is important for anyone building a system where the password is transmitted, to have ways of securing the communication channel" (SSL on IIS). — [Configure HTTP Access](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- Client-library note: "The MSO-ID (Microsoft online Live ID) identity provider isn't supported to authenticate federated HTTP connections. This limitation only impacts SQL Server Analysis Services for HTTP connections hosted on IIS." and "TLS 1.1 and TLS 1.0 aren't supported over HTTP channels. TLS 1.2 is the minimum" (19.114.8+). — [client libraries](https://learn.microsoft.com/en-us/analysis-services/client-libraries)

**Python auth libraries**
- requests-ntlm 1.3.0 (released 2024-06-09, Python >=3.8) depends on `requests` and `pyspnego`; usage `HttpNtlmAuth('domain\\username','password')`; "HttpNtlmAuth can be used in conjunction with a Session in order to make use of connection pooling. Since NTLM authenticates connections, this is more efficient." — [requests-ntlm on PyPI](https://pypi.org/project/requests-ntlm/); [GitHub](https://github.com/requests/requests-ntlm)
- pyspnego handles "SPNEGO (Negotiate, NTLM, Kerberos) and CredSSP authentication"; Kerberos on Linux needs optional `python-gssapi` and `pykrb5` plus system `krb5` dev libs; "NTLM acceptor support is less thoroughly tested than the GSSAPI implementation" (acceptor = server side). — [jborean93/pyspnego](https://github.com/jborean93/pyspnego)
- requests-kerberos 0.15.0 (released 2024-06-03): mutual auth modes REQUIRED/OPTIONAL/DISABLED; `delegate=True` sets GSS_C_DELEG_FLAG; `principal` picks a ccache; new in 0.15.0 `password` with `principal` requests a TGT; Linux needs `gcc python3-dev libkrb5-dev` (Debian) / `krb5-devel` (EL); 0.13.0 "changed the Kerberos dependencies to pyspnego". — [requests-kerberos on PyPI](https://pypi.org/project/requests-kerberos/)
- Legacy `xmla` 0.7.2 (Aug 2013, Python 2, alpha) shows the intended pattern: `location="https://my-as-server/olap/msmdpump.dll"` with `sslverify` and "you will need a valid kerberos principal of course"; Kerberos "need is detected automatically". — [xmla on PyPI](https://pypi.org/project/xmla/)

**NTLM keep-alive / connection reuse**
- Microsoft IIS guidance: NTLM is connection-based; the server treats later requests on the same keep-alive connection as the already-authenticated user. — [Are you seeing 401's too often for HTTP web requests?](https://learn.microsoft.com/cs-cz/archive/blogs/saurabh_singh/are-you-seeing-401s-too-often-for-http-web-requests)
- curl maintainer Daniel Stenberg: NTLM "needs several requests on one connection"; forbidding reuse closes the connection after the first request "even if it was the first one in a NTLM-series". — [curl-library mail](https://curl.se/mail/lib-2014-08/0184.html)
- curl bug: "NTLM authenticates connections and not individual requests"; "negotiating an NTLM authenticated connection always begins with a 401 response". — [curl #4499](https://curl.haxx.se/bug/?i=4499)
- Proxies: Squid/nginx maintainers note NTLM through a proxy needs connection pinning because "the same connection for the same client" isn't guaranteed. — [squid-dev](https://www.squid-cache.org/mail-archive/squid-dev/200503/0150.html); [nginx list](https://mailman.nginx.org/pipermail/nginx/2012-January/031308.html)
- Load balancer: dropping the connection "breaks multi-stage authentication protocols such as NTLM, which require the TCP socket to remain persistent across the 401 challenge and subsequent authorization requests." — [Broadcom KB 444070](https://knowledge.broadcom.com/external/article/444070/ntlm-authentication-fails-with-401-unaut.html)
- requests-ntlm open issues by title: "#103 [Bug] Cannot working if under retry" (2018), "#128 Error 401 after redirecting" (2022), "#61 401 error" (2015), "#104 Problem with NTLM-authenticate proxy for HTTPS CONNECT" (2018). — [requests-ntlm issues](https://github.com/requests/requests-ntlm/issues?q=is%3Aissue+keep-alive+OR+session+OR+connection)

### Inferences
- For Windows Integrated on the pump: `requests.Session()` + `HttpNtlmAuth` (NTLM, works with username/password from Linux without a domain join) or `HTTPKerberosAuth` (needs krb5 config + ticket/keytab, or 0.15.0 `principal`+`password`). Each new pooled TCP connection re-runs the 3-leg NTLM handshake; with urllib3 retries/redirects, or an intermediary without connection affinity, expect spurious 401s.
- For a multi-tenant server-side Python service that must act as many end users, NTLM/Kerberos per-connection auth is awkward (one Session per user identity, or per request); Basic over TLS (per-user stored or forwarded creds) or a single service identity + `EffectiveUserName` (see KQ4) is operationally simpler.

### Gaps
- No requests-ntlm issue explicitly titled about keep-alive was found (page lists titles only); the gotcha is documented at the protocol level (curl, IIS) rather than in requests-ntlm itself.
- Whether `msmdpump.dll` honors NTLM over HTTP/2 or behind common reverse proxies was not covered by sources.

---

## KQ4. EffectiveUserName requirements and behavior over the HTTP pump; Roles; CustomData

### Takeaway
`EffectiveUserName` (domain\user) makes SSAS evaluate roles/RLS as that user, but the connecting identity must be an SSAS server administrator; over the pump with Basic auth, Microsoft states the app pool identity must be an SSAS server admin, confirming it works through msmdpump. `Roles` and `CustomData` are per-connection properties that also pass through the XMLA `PropertyList`.

### Cited Findings
- "EffectiveUserName — Use when a user identity must be impersonated on the server. For SSAS, specify in a domain\user format. For Azure AS and Power BI Premium, specify in UPN format. To use this property, the caller must have administrative permissions in Analysis Services." — [Connection string properties](https://learn.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services)
- Over the pump: "Basic authentication — Add to the Membership list the Windows user or group accounts that will be passed on the connection string. In addition, if you are passing credentials via EffectiveUserName on the connection string, then the application pool identity must have administrator rights on the Analysis Services instance. In SSMS, right-click the instance | Properties | Security | Add. Enter the application pool identity. If you used the built-in default identity, the account is specified as IIS AppPool\DefaultAppPool." — [Configure HTTP Access](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- Windows auth over pump: "Assuming Kerberos constrained delegation is used, the only accounts that need permissions are the Windows user and group accounts requesting access. No permissions are necessary for the application pool identity." — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- "EffectiveUserName is a connection string property used for passing identity information to Analysis Services ... It can also be used in custom applications or scripts that perform operations on an Analysis Services instance." — [Authentication methodologies (2012 archive)](https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2012/dn141154(v=sql.110))
- SharePoint precedent: EffectiveUserName "enables per-user identity without the requirement to configure Kerberos delegation"; "The PerformancePoint Services application pool account must be an Analysis Services Administrator." — [Use EffectiveUserName in PerformancePoint Services](https://learn.microsoft.com/en-nz/sharePoint/administration/use-effectiveusername-in-performancepoint-services); [Use Analysis Services EffectiveUserName in SharePoint Server](https://learn.microsoft.com/en-nz/sharePoint/administration/use-analysis-services-effectiveusername-in-sharepoint-server)
- Roles: "Specify a comma-delimited list of predefined roles to connect to a server or database using permissions conveyed by that role. If this property is omitted, all roles are used ... Setting the property to an empty value, for example, Roles=' ' means the client connection has no role membership. An administrator using this property connects using the permissions conveyed by the role." — [Connection string properties](https://learn.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services)
- CustomData: "A string value to be passed to the engine in the connection string and then retrieved by using the CustomData() MDX function or CUSTOMDATA() DAX function. Most commonly used to pass a userID or username as a value, and then specify that value in the definition of a security expression." — [same](https://learn.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services)
- Impersonation Level values: Anonymous, Identify, Impersonate (default), Delegate. SSPI package selection: Negotiate, Kerberos, NTLM, Anonymous User. — [same](https://learn.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services)

### Inferences
- The practical per-user RLS pattern for a Linux service is: pump with Basic (or Anonymous bound to a domain service account) over HTTPS; the IIS identity that reaches SSAS (the Basic user, or the app-pool/anonymous account) is made an SSAS server admin; every SOAP request carries `<PropertyList><EffectiveUserName>DOMAIN\enduser</EffectiveUserName>` (and optionally `<Roles>`/`<CustomData>`). This avoids per-user Windows credentials and Kerberos delegation entirely, at the cost of a highly privileged service account.
- Microsoft documents these as "connection string properties"; in raw XMLA they travel in `Execute/Properties/PropertyList` (the connection-string form is just how ADOMD/MSOLAP populate that list). The XMLA Execute example shows `<PropertyList><DataSourceInfo>..</DataSourceInfo><Catalog>..</Catalog><Format>..</Format>` in exactly that position.

### Gaps
- No source explicitly lists `EffectiveUserName`, `Roles`, `CustomData` as XMLA PropertyList element names (the documentation describes them as connection-string properties). Confirm against a live server or the DISCOVER_PROPERTIES rowset.

---

## KQ5. Performance and size limits of XMLA Tabular responses for large DAX results; pagination

### Takeaway
Microsoft documents only one hard server-side row limit relevant to DAX over XMLA (`MaxIntermediateRowsetSize`, default 1,000,000, msmdsrv.ini DAX/DQ section, mainly DirectQuery), plus pump-level `SessionTimeout`/`ConnectionPoolSize` and client `Timeout`. No published size/throughput numbers for msmdpump Tabular responses were found; pagination via TOPN-with-key-filter is the community pattern.

### Cited Findings
- "For very large rowsets, such as those returned by a DAX query in DirectQuery models, the default of one million rows could be insufficient. To increase the upper limit, specify the MaxIntermediateRowSize setting ... error: 'The result set of a query to external data source has exceeded the maximum allowed size of '1000000' rows.'" Table: "MaxIntermediateRowsetSize | 1000000 | Maximum number of rows returned in a DAX query. For SSAS, manually add this entry to the msmdsrv.ini file" under `<DAX><DQ><MaxIntermediateRowsetSize>`. — [Analysis Services DAX properties](https://learn.microsoft.com/en-us/analysis-services/server-properties/dax-properties)
- Pump: `<SessionTimeout>3600</SessionTimeout><ConnectionPoolSize>100</ConnectionPoolSize>`. — [Configure HTTP Access](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- Client-side knobs: "Timeout — Specifies how long (in seconds) the client library waits for a command to complete"; "Connect Timeout"; "DbpropMsmdRequestMemoryLimit — Overrides the Memory\QueryMemoryLimit server property value for a connection"; "Protocol Format ... Default, XML, or Binary ... OLE DB provider can format requests and responses in binary or compressed format. AMO and ADOMD.NET format the requests as Text, but accept responses in binary or compressed format"; "Transport Compression ... None, Compressed. ... Compressed uses XPRESS compression"; "Compression Level 0-9". — [Connection string properties](https://learn.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services)
- Community pagination pattern (Power BI REST, but applicable to DAX): SKIP "is not supported"; workaround is TOPN with a filter on the last key of the previous page; the REST executeQueries API caps at 100,000 rows / 1,000,000 values (cloud-only limit). — [Fabric community: SKIP alternative](https://community.fabric.microsoft.com/t5/Developer/SKIP-Alternative-for-EXECUTE-QUERIES-API-pagination/m-p/3674013/highlight/true)
- Anecdotal timeouts: a ~10-minute DAX query on a paginated report produced "The connection either timed out or was lost"; fix was raising dataset query timeout. — [Fabric community](https://community.fabric.microsoft.com/t5/Developer/Slow-paginated-report-The-connection-either-timed-out-or-was/m-p/4606276/highlight/true)
- XMLA Rowset shape (what a plain-text Tabular response costs): each row is `<row>` with one element per column, element name = encoded column name; nulls are either a missing element or `xsi:nil='true'`; schema block `<xsd:schema ...>` with `sql:field` attributes precedes rows. — [Rowset Data Type (XMLA)](https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2012/ms187127(v=sql.110))

### Inferences
- A raw Python SOAP client receives verbose text XML (repeated element names per cell) unless it negotiates binary XML/XPRESS — formats that are proprietary and implemented only by MSOLAP/ADOMD. Expect response size to be several times the CSV equivalent; stream-parse (`iterparse`) and page with `TOPN`/key filters rather than pulling whole tables.
- The `MaxIntermediateRowsetSize` cap mostly bites DirectQuery models; for import models the practical ceiling is memory/`QueryMemoryLimit` and HTTP timeouts (IIS request timeout, pump `SessionTimeout`, client `Timeout`).

### Gaps
- No benchmark or Microsoft statement on maximum response size or throughput through msmdpump was found. The old "MaxRows"-style client property was not located in current docs.

---

## KQ6. Python libraries that wrap XMLA/DAX and their 2025-2026 maintenance status

### Takeaway
There is no maintained, Linux-native Python XMLA/DAX client: `xmla` (olap.xmla) is Python 2 and last released 2013 (archived repo); `pyadomd` is a pythonnet wrapper around Windows ADOMD.NET with open issues through 2025 and unanswered .NET Core/Linux requests; `pymsasdax` is COM/MSOLAP (Windows-only); `ssas_api`/`pyxmla`/`pydax`/`pbi-dax` were not found on PyPI/GitHub. A hand-rolled `requests` + `lxml` SOAP client is the realistic option.

### Cited Findings
- `xmla` 0.7.2 released August 11, 2013; classifier "Programming Language :: Python :: 2"; Development Status "3 - Alpha"; relies on suds and requests 1.2.3; Kerberos optional. — [xmla on PyPI](https://pypi.org/project/xmla/)
- `alloytech/olap` (the olap.xmla source) GitHub repo "is archived and read-only". — [alloytech/olap](https://github.com/alloytech/olap)
- `pyadomd`: "A pythonic approach to query SSAS data models"; examples prepend a Windows ADOMD.NET folder to `sys.path`; PyPI badge "windows-10". Open issues: #3 NetCore (2021, unresolved), #12 Python 3.9/3.10 (2021), #24 "No Credentials Available in the Security Package" (2023), #27 FileNotFoundException (Mar 2025), #29 pip install deprecation error (Sep 2025); 12 open issues. — [pyadomd on PyPI](https://pypi.org/project/pyadomd); [issues](https://github.com/S-C-O-U-T/Pyadomd/issues)
- `pymsasdax` (grenzi): "a small Python module for running DAX queries against Microsoft Analysis Services, using COM Interop"; "assumes that the MSOLAP client is installed"; "alpha-quality"; versions 2023.1013-2023.1018. — [pymsasdax on PyPI](https://pypi.org/project/pymsasdax); [GitHub](https://github.com/grenzi/pymsasdax)
- `kthejoker/python_xmla`: Databricks notebook loading `Microsoft.AnalysisServices.AdomdClient.dll`, `Microsoft.AnalysisServices.Runtime.Core.dll`, `Microsoft.AnalysisServices.Runtime.Windows.dll`, `Microsoft.Identity.Client.dll`, `Microsoft.IdentityModel.Abstractions.dll` from a SQL Server 2019 install with `apt-get install dotnet-runtime`; targets Power BI XMLA (Entra). 3 commits. — [kthejoker/python_xmla](https://github.com/kthejoker/python_xmla)
- `ssas-mcp-server` (NexusAI-Solutions): uses ADOMD.NET "which requires .NET Framework on Windows"; another SSAS MCP server "drives ADOMD.NET via pythonnet and is Windows-only by construction". — [mcpservers.org listing](https://mcpservers.org/servers/nexusai-solutions/ssas-mcp-server); [glama listing](https://glama.ai/mcp/servers/afnmcu4564)
- `netas-xmla-with-dax` exists on PyPI (a fork of olap.xmla with DAX); details not fetched. — [PyPI](https://pypi.org/project/netas-xmla-with-dax/)
- Fabric community guidance for Python DAX: "you will need to use an XMLA-compatible client library (such as pyadomd)" — cloud/service-principal context. — [Fabric community](https://community.fabric.microsoft.com/t5/DAX-Commands-and-Tips/DAX-standalone-query-execution-using-python/m-p/4994417/highlight/true)
- Searches for `ssas_api`, `pyxmla`, `pydax`, `pbi-dax` returned no matching packages. — [search result summary](https://pypi.org/project/xmla/0.7/)

### Inferences
- The SOAP surface needed is small (two methods: `Discover`, `Execute`; one envelope shape), so writing a ~200-line `requests`/`httpx` + `lxml` client with pluggable auth (`HTTPBasicAuth`, `HttpNtlmAuth`, `HTTPKerberosAuth`) is lower-risk than depending on any existing Python package.

### Gaps
- `netas-xmla-with-dax` maintenance status and Python 3 support were not verified.

---

## KQ7. SOAP XMLA request/response shape for DAX (Execute/Statement, Format=Tabular) and Discover rowsets for Tabular metadata

### Takeaway
DAX goes in `Execute/Command/Statement` with `PropertyList/Format=Tabular` (and `Catalog`); the result is an `urn:schemas-microsoft-com:xml-analysis:rowset` `<root>` with an inline XSD and `<row>` elements whose child element names are the column names with invalid XML characters encoded as `_xHHHH_` (e.g. `Order_x0020_Details`), decodable via the `sql:field` attribute. Metadata comes from `Discover` rowsets: `DBSCHEMA_CATALOGS`, `MDSCHEMA_*`, `DISCOVER_CSDL_METADATA`, and the compatibility-level-1200+ `TMSCHEMA_*` DMVs (36 of them).

### Cited Findings
- Execute: "Namespace urn:schemas-microsoft-com:xml-analysis; SOAP Action 'urn:schemas-microsoft-com:xml-analysis:Execute'"; syntax `<Execute><Command>...</Command><Properties>...</Properties><Parameters>...</Parameters></Execute>`; "returns any resulting data using either the XMLA Rowset data type (for tabular result sets) or the XMLA MDDataSet data type (for multidimensional result sets.)"; example PropertyList: `<DataSourceInfo>Provider=MSOLAP;Data Source=local;</DataSourceInfo><Catalog>...</Catalog><Format>Multidimensional</Format><AxisFormat>ClusterFormat</AxisFormat>`. — [Execute Method (XMLA)](https://learn.microsoft.com/en-us/analysis-services/xmla/xml-elements-methods-execute)
- "TMSL communicates with Analysis Services through the XMLA protocol, where the XMLA.Execute method accepts both JSON-based statement scripts in TMSL as well as the traditional XML-based scripts in Analysis Services Scripting Language" — i.e. the same `Execute` carries DAX, MDX, TMSL and ASSL. — [TMSL Reference](https://learn.microsoft.com/en-us/analysis-services/tmsl/tabular-model-scripting-language-tmsl-reference)
- Rowset: "Namespace urn:schemas-microsoft-com:xml-analysis:rowset"; `<root xmlns="urn:schemas-microsoft-com:xml-analysis:rowset"><!-- Optional schema elements --><row>...</row></root>`; "For column names that contain XML name characters that are not valid according to the XML 1.0 specification, XMLA uses the corresponding hexadecimal values to encode Unicode characters that are not valid. The hexadecimal values are escaped as _xHHHH_, where HHHH stands for the four-digit hexadecimal UCS-2 code for the character in the most-significant bit first order. For example, XMLA encodes the name, 'Order Details,' as Order_x0020_Details"; schema carries `<xsd:element name="Order_x0020_Details" type="string" sql:field="Order Details" />` ("sql:field attribute is in the 'urn:schemas-microsoft-com:xml-sql' namespace"); nulls: "A missing column element implies that the column is null. The column element may use the xsi:nil='true' attribute". — [Rowset Data Type (XMLA)](https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2012/ms187127(v=sql.110))
- Schema rowsets overview: "The Microsoft XML for Analysis (XMLA) provider includes schema rowsets that return metadata about server state, activity, and objects ... you can run a Dynamic Management View (DMV) query against most schema rowsets." — [XML for Analysis Schema Rowsets (2012 archive)](https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2012/ms126221(v=sql.110))
- `DISCOVER_CSDL_METADATA` "returns an XML definition of a data source to a client that can consume a tabular or PowerPivot model" (quoted from Microsoft XMLA docs via search summary). — [search summary referencing Microsoft XMLA docs](https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2012/ms126221(v=sql.110))
- TMSCHEMA DMVs: "With the new compatibility level 1200 SSAS Tabular models came some new DMVs"; "While there are 36 TMSCHEMA DMVs, the main DMVs you will want to check out for model documentation purposes are: TMSCHEMA_COLUMNS, TMSCHEMA_MEASURES, TMSCHEMA_MODEL, TMSCHEMA_RELATIONSHIPS, TMSCHEMA_TABLES"; "not documented at this time (as of 3 Oct 2016)". — [SQLServerCentral: Documenting your Tabular or Power BI model](https://www.sqlservercentral.com/blogs/documenting-your-tabular-or-power-bi-model)
- DMV syntax example: "the measures view will query the $SYSTEM.TMSCHEMA_MEASURES and return an entire table with 19 columns". — [SumProduct INFO.MEASURES](https://sumproduct.com/blog/the-a-to-z-of-dax-functions-info-measures/)
- Rowset example columns for `DBSCHEMA_CATALOGS`: `CATALOG_NAME`, `DESCRIPTION`, `ROLES`, `DATE_MODIFIED`. — [Rowset Data Type](https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2012/ms187127(v=sql.110))
- Character encoding of requests: "Character Encoding — Valid values are Default or UTF-8 (these are equivalent), and UTF-16." — [Connection string properties](https://learn.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services)

### Inferences
- A DAX request envelope is: SOAP `Envelope/Body/Execute` (xmlns `urn:schemas-microsoft-com:xml-analysis`) with `Command/Statement` = `EVALUATE ...`, `Properties/PropertyList` = `Catalog`, `Format=Tabular`, optional `EffectiveUserName`/`Roles`/`CustomData`/`Timeout`; header `SOAPAction: "urn:schemas-microsoft-com:xml-analysis:Execute"`, `Content-Type: text/xml`. DAX column names like `Sales[Amount]` or `[Total Sales]` will come back encoded (`Sales_x005B_Amount_x005D_`, `_x005B_Total_x0020_Sales_x005D_`); decode with the `sql:field` attribute from the inline schema rather than by regex.
- TMSCHEMA_* DMVs (run as `Execute/Statement` = `SELECT * FROM $SYSTEM.TMSCHEMA_TABLES`, Tabular format) are the best source of Tabular metadata (tables, columns, measures with DAX expressions, relationships) for 1200+ models; `DISCOVER_CSDL_METADATA` gives an EDM/CSDL view of a model; `MDSCHEMA_*` gives the multidimensional projection (cube = model, measure group = table).

### Gaps
- Current Microsoft Learn pages for the full rowset list and individual `TMSCHEMA_*` / `DISCOVER_XML_METADATA` rowsets could not be fetched (404 on several URL variants); the archived 2012 page text was truncated before its table. `DISCOVER_XML_METADATA` existence/semantics therefore unverified from a primary source.
- The exact XMLA `Format=Tabular` DAX response was not reproduced from a primary source; the shape is inferred from the generic Rowset data type doc.

---

## KQ8. Kerberos constrained delegation when the pump is on a different box than SSAS

### Takeaway
If IIS (pump) and SSAS are on different machines and you use Windows Integrated auth with per-user identity, NTLM fails on the second hop; you must register SSAS SPNs and configure Kerberos constrained delegation for the IIS app pool identity, or sidestep with Basic+TLS (credentials re-presented by the pump) or EffectiveUserName.

### Cited Findings
- "IIS and Analysis Services on different computers — ... This topology adds a double-hop authentication step, where credentials must flow from the client to the web server, and on to the backend Analysis Services server. If you are using Windows credentials and NTLM, you'll get an error because NTLM doesn't allow delegation of client credentials to a second server. The most common solution is to use Basic authentication with Secure Sockets Layer (SSL) ... A more straightforward approach might be to enable Kerberos and configure Analysis Services constrained delegation". — [Configure HTTP Access](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- "IIS and Analysis Services on the same computer — ... allows you to use the default configuration (where the server name is localhost), the local Analysis Services OLE DB provider, and Windows integrated security with NTLM." — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)
- "Kerberos constrained delegation is required if the user identity has to flow over multiple computer connections (known as 'double-hop')." "Delegation is not required if the connection to Analysis Services is a single hop, or your solution uses stored credentials". Steps: "Step 1: Verify that accounts are suitable for delegation (not marked sensitive) ... Step 2: Register the SPN ... Step 3: Configure constrained delegation ... specifying the Analysis Services SPN as the specific service for which delegation is allowed ... Step 4: Test connections" (SQL Server Profiler Audit Login shows the identity). — [Configure Analysis Services for Kerberos constrained delegation](https://learn.microsoft.com/en-us/analysis-services/instances/configure-analysis-services-for-kerberos-constrained-delegation)
- "Generally, all accounts and servers must belong to the same Active Directory domain or to trusted domains in the same forest. However, because Windows Server 2012 supports delegation across domain boundaries, you can configure Kerberos constrained delegation across a domain boundary if the domain functional level is Windows Server 2012." — [same](https://learn.microsoft.com/en-us/analysis-services/instances/configure-analysis-services-for-kerberos-constrained-delegation)
- "In multi-tier solutions, the single hop restriction of NTLM can be a major constraint. The user identity making the request can be impersonated on exactly one remote server, but travels no further." "Only Kerberos with delegation allows the original user identity to flow across multiple computer connections". — [Authentication methodologies (2012 archive)](https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2012/dn141154(v=sql.110))
- Client side: `requests-kerberos` `delegate=True` "enable credential delegation (GSS_C_DELEG_FLAG)" with a warning to delegate only to trusted servers; CData driver `AuthScheme=KERBEROSDELEGATION` with `KerberosSPN`. — [requests-kerberos](https://pypi.org/project/requests-kerberos/); [CData](https://www.cdata.com/kb/tech/ssas-odbc-python-linux.rst)
- Microsoft's Windows-auth role guidance over the pump assumes KCD: "Assuming Kerberos constrained delegation is used, the only accounts that need permissions are the Windows user and group accounts requesting access." — [Configure HTTP Access](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)

### Inferences
- Simplest robust topology for a Linux backend: install the pump on the SSAS host itself (localhost ServerName), so Windows Integrated (NTLM or Kerberos) from Linux is single-hop and needs no delegation. If IIS must be separate, prefer Basic+HTTPS (or a service identity + EffectiveUserName) over KCD, which requires AD admin work (SPNs `MSOLAPSvc.3/host`, delegation tab) and Linux krb5 configuration.

### Gaps
- The SPN registration page (`setspn -S MSOLAPSvc.3/...`) was not fetched; exact SPN formats are therefore not quoted here.

---

## KQ9 (scope note). The Power BI Report Server Analysis Services engine as a target

### Takeaway
Community and Microsoft-affiliated forum replies describe PBIRS's Analysis Services as an internally managed embedded engine (msmdsrv under `PBIRS\ASEngine\workspaces`, dynamic/configured port) that is "only used for running .pbix based reports" and cannot be reached from other machines or have custom models deployed; connecting to it externally is undocumented and unsupported. Full threads could not be fetched (403), so treat as unverified.

### Cited Findings
- A forum reply states "the dll for this installed in your ReportServer\bin\msmdlocal directory, and these are the 'hidden' instances behind the PBI-SSRS reports"; a Microsoft-affiliated responder says "the embedded engine is only used for running .pbix based reports. You cannot connect to that from other machines and you cannot deploy custom models to that instance." Another post points to `msmdsrv.ini` in `C:\Program Files\Microsoft Power BI Report Server\PBIRS\ASEngine\workspaces` with `<Port>5132</Port>` as an example and suggests local-admin SSMS connection may work ("remote is not allowed but it could be about another change in .ini file only"). — [Fabric community: Needed instances for PowerBI report server](https://community.fabric.microsoft.com/t5/Report-Server/Needed-instances-for-PowerBI-report-server/td-p/445068); [Fabric community: Is SSAS service included in Power BI Report Server](https://community.fabric.microsoft.com/t5/Report-Server/Is-SSAS-service-included-in-Power-BI-Report-Server-on-premise/td-p/754288)
- "Power BI Report Server also runs hidden Analysis Services (SSAS tabular) instance in the background"; approach: list msmdsrv processes, netstat PID to port. — [Fabric community: Accessing PBIRS data from other applications](https://community.fabric.microsoft.com/t5/Report-Server/Accessing-Power-BI-Report-Server-data-from-other-applications/td-p/478825)
- The msmdpump doc says "You can configure HTTP access for any supported version or edition of Analysis Services, running either tabular mode or multidimensional mode. Local cubes are an exception." — [Configure HTTP Access](https://learn.microsoft.com/en-us/analysis-services/instances/configure-http-access-to-analysis-services-on-iis-8-0)

### Inferences
- A supported design for PBIRS-hosted models is to query a real SSAS Tabular instance (which PBIRS reports can use via live connection), not the embedded engine. Pointing `msmdpump.ini` `<ServerName>localhost:<port>` at the PBIRS embedded engine is technically conceivable but unsupported, port can change, and databases are managed by PBIRS.

### Gaps
- All PBIRS statements come from search snippets of community threads (pages returned 403); no Microsoft documentation statement on the embedded engine's accessibility was found.

---

## KQ10 (scope note). ODBC/OLE DB (MSOLAP) and vendor drivers

### Takeaway
MSOLAP is a native Windows OLE DB provider (Windows installer only); no Linux build exists. The only Linux ODBC/JDBC options found are CData's SSAS drivers, which are HTTP/XMLA clients to `msmdpump.dll` supporting Basic/Digest/NTLM/Negotiate/Kerberos-delegation; an insightsoftware/Simba "Analysis Services" Linux driver was not found.

### Cited Findings
- "Analysis Services OLE DB Provider (MSOLAP) is the native client library ... It must be installed on computers used to access Analysis Services data." Downloads: "MSOLAP (amd64) 17.0.96.20 / MSOLAP (x86)" Windows Installer only; "The Windows Installer downloads for ... AMO/TOM and ADOMD are no longer being updated and will not be available after Dec. 31st 2024." — [client libraries](https://learn.microsoft.com/en-us/analysis-services/client-libraries)
- CData ODBC on Linux: unixODBC + `.deb/.rpm`, `cdata.odbc.ssas.ini` `DriverManagerEncoding` UTF-16, DSN in `/etc/odbc.ini`, pyodbc; `URL=http://localhost/OLAP/msmdpump.dll`; AuthScheme Basic/Digest/NTLM/NEGOTIATE/KERBEROSDELEGATION; "free trial" only licensing note. — [CData KB](https://www.cdata.com/kb/tech/ssas-odbc-python-linux.rst)
- CData JDBC: URL `jdbc:ssas:` pointing at msmdpump.dll, driver class `cdata.jdbc.ssas.SSASDriver`; NTLM "Set the Windows User and Password and set AuthScheme to 'NTLM'"; Kerberos `NEGOTIATE`, delegation `KERBEROSDELEGATION` with `KerberosSPN`. — [CData JDBC getting started](https://www.cdata.com/kb/tech/ssas-jdbc-setup.rst)
- Searches for an insightsoftware/Simba Analysis Services ODBC/JDBC driver returned only other Simba connectors (Oracle, SQL Server, Cassandra, Databricks); no SSAS product surfaced. — [insightsoftware drivers](https://insightsoftware.com/drivers/databricks-odbc-jdbc-driver/)

### Inferences
- Vendor drivers do not remove the IIS pump prerequisite; they add licensing cost and a C/Java runtime in exchange for SQL-over-XMLA convenience, which is of little value when the goal is to send DAX verbatim.

### Gaps
- CData licensing terms/pricing not quoted (only "free trial" statement found). Simba/insightsoftware SSAS driver existence unconfirmed.
