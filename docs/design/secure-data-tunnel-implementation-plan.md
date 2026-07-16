# Secure Data Tunnel — Implementation plan 


## Phase 1: Initiate Data Edge Agent development
Steps to implement
1. Create a new python module under root directory, call it data_plane  
2. Inside data_plane module create data_edge_agent module
3. Create the data_edge_agent module sceleton, files layout, start up logic 
4. Create sample configuration file
5. Implement NATS.io logic, connecting to the subject reading response and writing it into the logs

Verification
1. Make sure data_edge_agent is up and running 

## Phase 2: Create PostgreSQL data source 
Steps to implement 
1. Under `data_plane/data_edge_agent` create a new directory `data_sources` 
2. Follow the `backend/app/data_sources/clients` convention and implement `postgresql_client.py`
3. Update `config.example.yaml` and include sample PostgreSQL data source configuration 
4. Implement advertisements method

Verification 
1. Start data_edge_agent and connect it to the nats.io instance 
2. data_edge_agent should publish all their connection to the subject   
3. start a dummy client connect to the nats.io subject, and receive the advertisement message 
4. print the advertisement result as a proof that advertisement works