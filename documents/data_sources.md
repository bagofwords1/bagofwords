# Data Sources

User can integrate to third-party data sources, such as databases, services, and files. The list of supported data sources is defined in the `DATA_SOURCE_DETAILS` list in the `models/data_source.py` file.

## Databases
User can connect to a variety of databases, such as MSSQL, MySQL, PostgreSQL, BigQuery, Snowflake, etc. The LLM agent will use the data source client to connect to the data source and execute queries. 

## Services
Besides databases, user can also connect to services, such as AWS Cost Explorer, Salesforce, etc. For each service, we will have: 
- a client to connect and query to the service
- a schema to describe the data structure of the service
- an output schema to describe the data structure of the output of the service


### Key files:

#### Client
Each client needs to inherit the `DataSourceClient` class, that is in the `data_sources/clients/base.py` file.

#### Schema
- `backend/app/schemas/data_source_schema.py`

Eeach data source eneds to have a schema class. For credentials, we will have a separate class.

```python
# PostgreSQL
class PostgreSQLCredentials(BaseModel):
    user: str
    password: str

class PostgreSQLConfig(BaseModel):
    host: str
    port: int = Field(5432, ge=1, le=65535)
    database: str

```

#### Output Schema
```
TODO
```

#### Generating conversation starters and a summary for a data source
The system will generate conversation starters and a summary for a data source given a working client (schema). It is using `app/ai/agents/data_source_agent.py` file.

## Files
User can process different file types:

### Excel
Excel files can be analyzed to extract schema, fields and metrics. The LLM agent will identify cell locations, data types and field orientations.

Key files:
- `backend/app/ai/agents/excel/excel.py`

### PDF 
PDF documents can be processed to extract text content and generate semantic tags. The LLM agent converts content to HTML and identifies key information.

Key files:
- `backend/app/ai/agents/doc/doc.py`

Both agents require a File object and LLMModel instance to function.




---
--
## Tests
Running backend tests
```bash
cd backend
pytest --log-cli-level=INFO  tests/test_clients.py 
```

## Setting up clients
Install ODBC and drivers (for Mssql database)

```bash
# Update the package list
sudo apt update

# Install unixODBC and its development library
sudo apt install -y unixodbc unixodbc-dev curl gnupg2

# Add the Microsoft repository
curl https://packages.microsoft.com/keys/microsoft.asc | sudo apt-key add -
curl https://packages.microsoft.com/config/ubuntu/20.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list

# Update the package list again
sudo apt update

# Install the ODBC Driver 18 for SQL Server
sudo ACCEPT_EULA=Y apt install -y msodbcsql18

# Verify that the driver is installed
odbcinst -q -d

# Test a DSN-less connection using isql (replace placeholders with actual values)
isql -v "DRIVER={ODBC Driver 18 for SQL Server};SERVER=your_server;DATABASE=your_database;UID=your_username;PWD=your_password"
```

Install Oracle DB required software
```bash
# Download Oracle Instant Client
wget https://download.oracle.com/otn_software/linux/instantclient/2360000/instantclient-basic-linux.x64-23.6.0.24.10.zip

# Extract the Oracle Instant Client
sudo mkdir -p /opt/oracle
sudo unzip instantclient-basic-linux.x64-23.6.0.24.10.zip -d /opt/oracle

# Set environment variables
echo "export LD_LIBRARY_PATH=/opt/oracle/instantclient_23_6:\$LD_LIBRARY_PATH" >> ~/.bashrc
echo "export PATH=/opt/oracle/instantclient_23_6:\$PATH" >> ~/.bashrc
source ~/.bashrc

# Install required libraries
sudo apt-get install libaio1

# Verify the installation
ls /opt/oracle/instantclient_23_6

```


docker-compose file for testing databases
```yml

services:
  postgres:
    image: postgres:13
    container_name: local_postgres
    env_file:
      - .env
    ports:
      - "5432:5432"
    volumes:
      - ./postgres_data:/var/lib/postgresql/data

  mysql:
    image: mysql:8
    container_name: local_mysql
    env_file:
      - .env
    ports:
      - "3306:3306"
    volumes:
      - ./mysql_data:/var/lib/mysql


  sqlserver:
    image: mcr.microsoft.com/mssql/server:latest
    container_name: local_sqlserver
    ports:
      - "1433:1433"
    environment:
      ACCEPT_EULA: "Y"
      SA_PASSWORD: "YourStrongPassword123" # Avoiding special characters to prevent errors
    volumes:
      - sqlserver_data:/var/opt/mssql
      # - ./scripts:/scripts 


  oracledb:
    image: gvenzl/oracle-xe:latest
    container_name: local_oracledb
    ports:
      - "1521:1521"
    environment:
      ORACLE_PASSWORD: "YourStrongPassword123" # Password for Oracle DB
    volumes:
      - oracledb_data:/opt/oracle/oradata

  presto:
    image: trinodb/trino
    container_name: local_presto
    ports:
      - "8080:8080"
    environment:
      - PRESTO_NODE_ID=presto-coordinator
      - PRESTO_COORDINATOR=true
      - PRESTO_DISCOVERY_ENABLED=true
    volumes:
      - ./presto/etc:/etc/presto
      - ./presto/etc/catalog:/etc/catalog

volumes:
  sqlserver_data:
    driver: local
  oracledb_data:
    driver: local
```

Example connecting to a docker container :
```bash
docker exec -it local_oracledb  bash
sqlplus system/YourStrongPassword123@//127.0.0.1:1521/FREEPDB1
```