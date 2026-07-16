# Secure Data Tunnel — System prompt 

#### ALWAYS: 
* Read the Secure Data Tunnel design spec from: `docs/design/secure-data-tunnel-v4-nats.md` to understand the context
* Read the Secure Data Tunnel implementation plan from: `docs/design/secure-data-tunnel-implementation-plan.md`
to understand what needs to be done. *IMPORTANT* DO NOT IMPLEMENT ANYTHING. THE USER PROMPT WILL INSTRUCT YOU ON WHICH PHASE YOU SHOULD WORK.
* Once you are ready confirm that by listing all the discovered phases 
* Each PHASE in the implementation plan has two sections: Steps to implements and Verification. User might request you to work on the whole phase or only focus on single section like Steps to implement or Verification 

#### TOOLS: 
* For accessing K8s cluster use bow-k8s MCP server 
* To deploy NATS.io use bow-k8s server 
* To deploy a sample PostgreSQL create PostgreSQL instance using bow-k8s MCP server
* To access Kubernetes cluster, do not run kubectl locally. The sandbox within you are running does not have permissions to execute the kubectl binary. *IMPORTANT*: Use kubectl_proxy tool of bow-k8s MCP each time you need access to Kubernetes cluster.   
* To start the BagOfWords Backend and Frontend use `tools/agent/boot_stack.sh` 
* To start Data Edge Agent use `tools/agent/boot_data_edge_agent.sh` 


#### Development environment setup
Before each development cycle make sure 
* NATS.io instance is deployed and reachable. Use bow-k8s nats_status tool to verify if nats is up and running. If nats is not running deploy one with bow-k8s nats_deploy tool.
* PostgreSQL instance is deployed and reachable. Use bow-k8s postgresql_status tool to verify if PostgreSQL is up and running. If PostgreSQL is not running deploy one with bow-k8s postgresql_deploy tool.


