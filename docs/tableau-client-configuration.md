# Tableau Client Configuration Guide

This guide explains the access parameters required to connect to Tableau Server or Tableau Cloud using the Tableau Client.

## Required Parameters

### Server URL
- **Parameter**: `server_url`
- **Type**: String
- **Required**: Yes
- **Description**: The base URL of your Tableau Server or Tableau Cloud instance
- **Examples**:
  - Tableau Server: `https://tableau.yourcompany.com`
  - Tableau Cloud: `https://us-east-1.online.tableau.com` or `https://10az.online.tableau.com`

## Authentication Methods

You must provide **one** of the following authentication methods:

### Method 1: Personal Access Token (PAT) - Recommended

Personal Access Tokens are the recommended authentication method as they are more secure and don't expose passwords.

**Required Parameters**:
- **`pat_name`**: The name of your Personal Access Token
- **`pat_token`**: The secret token value

**How to create a PAT**:
1. Log in to Tableau Server/Cloud
2. Go to your account settings
3. Navigate to "Personal Access Tokens"
4. Click "Create new token"
5. Give it a meaningful name and copy the token value (you won't be able to see it again)

**Example Configuration**:
```json
{
  "server_url": "https://tableau.yourcompany.com",
  "site_name": "your-site-name",
  "pat_name": "bagofwords-integration",
  "pat_token": "your-secret-token-value"
}
```

### Method 2: Username and Password

**Required Parameters**:
- **`username`**: Your Tableau username
- **`password`**: Your Tableau password

**Example Configuration**:
```json
{
  "server_url": "https://tableau.yourcompany.com",
  "site_name": "your-site-name",
  "username": "john.doe@company.com",
  "password": "your-password"
}
```

## Site Configuration

### Site Name
- **Parameter**: `site_name`
- **Type**: String
- **Required**: No (required for multi-site Tableau deployments)
- **Description**: The content URL of your Tableau site
- **Default**: Empty string (uses the default site)

**How to find your site name**:
- The site name is the identifier in your Tableau URL
- Example: If your URL is `https://tableau.company.com/#/site/marketing/home`, your site name is `marketing`
- For Tableau Cloud, it's typically shown in the URL after `/site/`
- If you're using the default site, you can leave this empty or omit it

## Optional Parameters

### SSL Verification
- **Parameter**: `verify_ssl`
- **Type**: Boolean
- **Default**: `true`
- **Description**: Whether to verify SSL certificates when connecting
- **When to disable**: Only disable for development/testing with self-signed certificates. **Never disable in production**.

```json
{
  "verify_ssl": false  // Only for development
}
```

### Connection Timeout
- **Parameter**: `timeout_sec`
- **Type**: Integer
- **Default**: `30`
- **Description**: Request timeout in seconds for API calls
- **When to adjust**: Increase for slow networks or large data sources

```json
{
  "timeout_sec": 60  // For slower connections
}
```

### Project Filtering
- **Parameter**: `default_project_id`
- **Type**: String
- **Default**: None
- **Description**: Filter data sources to only include those from a specific project
- **Use case**: When you want to limit access to data sources within a particular project

**How to find project ID**:
1. Use Tableau's REST API or Metadata API
2. Navigate to the project in Tableau Server/Cloud
3. Check the URL - the project ID is in the URL path

```json
{
  "default_project_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

### Data Source Filtering
- **Parameter**: `include_datasource_ids`
- **Type**: Array of strings
- **Default**: Empty array (includes all data sources)
- **Description**: Explicitly specify which data sources to include by their LUIDs (unique IDs)
- **Use case**: When you want to expose only specific data sources

**How to find data source LUIDs**:
1. Use Tableau's REST API endpoint: `/api/{version}/sites/{site-id}/datasources`
2. Check the Metadata API
3. The LUID is the unique identifier (UUID v4 format)

```json
{
  "include_datasource_ids": [
    "12345678-abcd-1234-efgh-567890abcdef",
    "87654321-dcba-4321-hgfe-fedcba098765"
  ]
}
```

## Complete Configuration Examples

### Example 1: Tableau Cloud with PAT (Recommended)
```json
{
  "server_url": "https://us-east-1.online.tableau.com",
  "site_name": "mycompany",
  "pat_name": "bagofwords-prod",
  "pat_token": "AbCdEf123456==",
  "verify_ssl": true,
  "timeout_sec": 30
}
```

### Example 2: Tableau Server with Username/Password
```json
{
  "server_url": "https://tableau.internal.company.com",
  "site_name": "analytics",
  "username": "service-account@company.com",
  "password": "SecurePassword123!",
  "verify_ssl": true,
  "timeout_sec": 45
}
```

### Example 3: With Project and Data Source Filtering
```json
{
  "server_url": "https://tableau.company.com",
  "site_name": "sales",
  "pat_name": "bagofwords-sales",
  "pat_token": "XyZ789==",
  "default_project_id": "abc123-def456-ghi789",
  "include_datasource_ids": [
    "datasource-1-uuid",
    "datasource-2-uuid"
  ],
  "verify_ssl": true,
  "timeout_sec": 30
}
```

### Example 4: Default Site with PAT
```json
{
  "server_url": "https://tableau.company.com",
  "pat_name": "bagofwords-default",
  "pat_token": "TokenValue123==",
  "verify_ssl": true
}
```

## Required Permissions

To use the Tableau Client, your Tableau user account or Personal Access Token must have the following permissions:

### Minimum Required:
- **View** permission on the data sources you want to query
- **Connect** permission to data sources
- Access to the **Metadata API** (for schema discovery)
- Access to **VizQL Data Service** / **Headless BI** (for querying data)

### Recommended Permissions:
- **Explorer** or **Viewer** site role (minimum)
- **Interactor** role on projects containing the data sources

### For Service Accounts:
If using a dedicated service account for integration:
1. Create a service account with appropriate site role
2. Grant explicit permissions on required projects and data sources
3. Use PAT authentication for the service account
4. Rotate tokens regularly following your security policies

## Troubleshooting

### Connection Issues

**Error: "server_url is required"**
- Solution: Ensure `server_url` is provided and not empty

**Error: "Either PAT or username/password must be provided"**
- Solution: Provide either `pat_name` + `pat_token` OR `username` + `password`

**Error: "Failed to sign in to Tableau: HTTP 401"**
- Solutions:
  - Verify credentials are correct
  - Check if the account is active and not locked
  - For PAT: Ensure the token hasn't expired
  - Verify site name is correct

**Error: "Failed to sign in to Tableau: HTTP 404"**
- Solutions:
  - Verify the `server_url` is correct
  - Check if the `site_name` exists
  - Ensure the site is accessible

### SSL Certificate Issues

**Error: SSL certificate verification failed**
- Solutions:
  - For production: Ensure valid SSL certificates are installed
  - For development only: Set `verify_ssl: false` (not recommended for production)

### Timeout Issues

**Error: Request timeout**
- Solutions:
  - Increase `timeout_sec` value
  - Check network connectivity
  - Verify Tableau Server is responsive

## Security Best Practices

1. **Use Personal Access Tokens** instead of username/password
2. **Rotate tokens regularly** (e.g., every 90 days)
3. **Store credentials securely** - never commit tokens/passwords to version control
4. **Use environment variables** or secure secret management systems
5. **Enable SSL verification** in production environments
6. **Use service accounts** with minimum required permissions
7. **Monitor token usage** through Tableau's audit logs
8. **Revoke unused tokens** immediately

## Testing Your Configuration

After configuring the Tableau Client, you can test the connection:

```python
from app.data_sources.clients.tableau_client import TableauClient

client = TableauClient(
    server_url="https://your-tableau-server.com",
    site_name="your-site",
    pat_name="your-pat-name",
    pat_token="your-pat-token"
)

# Test connection
result = client.test_connection()
if result["success"]:
    print("✓ Connected successfully to Tableau")
    print(f"Details: {result.get('message')}")
else:
    print("✗ Connection failed")
    print(f"Error: {result.get('message')}")
```

## Additional Resources

- [Tableau REST API Documentation](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api.htm)
- [Personal Access Tokens](https://help.tableau.com/current/server/en-us/security_personal_access_tokens.htm)
- [Tableau Metadata API](https://help.tableau.com/current/api/metadata_api/en-us/index.html)
- [VizQL Data Service](https://help.tableau.com/current/api/vizql_data_service/en-us/index.html)
