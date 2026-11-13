#!/usr/bin/env bash
# Start every backing server Loop C uses, on the ports the loop scripts expect.
# Stop with: docker rm -f $(docker ps -aq --filter name=bow-)
set -e
WH=/tmp/bow-iceberg-wh; mkdir -p $WH; chmod 777 $WH
docker run -d --name bow-gcs -p 4443:4443 fsouza/fake-gcs-server:1.52.2 -scheme http -port 4443 -public-host 127.0.0.1:4443
docker run -d --name bow-azurite -p 10000:10000 mcr.microsoft.com/azure-storage/azurite:3.34.0 azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck --loose
docker run -d --name bow-ddb -p 8001:8000 amazon/dynamodb-local:2.5.4
docker run -d --name bow-cass -p 9042:9042 -e MAX_HEAP_SIZE=512M -e HEAP_NEWSIZE=128M cassandra:5.0
docker run -d --name bow-cb -p 8091-8097:8091-8097 -p 11210:11210 couchbase/server:community-7.6.2
docker run -d --name bow-cosmos -p 8081:8081 mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-preview --protocol https
docker run -d --name bow-dremio -p 9047:9047 dremio/dremio-oss:25.2
docker run -d --name bow-hive -p 10010:10000 -e SERVICE_NAME=hiveserver2 apache/hive:4.0.1
docker run -d --name bow-iceberg -p 8181:8181 -v $WH:$WH -e CATALOG_WAREHOUSE=file://$WH apache/iceberg-rest-fixture:1.9.1
