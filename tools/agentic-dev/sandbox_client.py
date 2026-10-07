from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxConnectionConfig, SandboxDirectConnectionConfig





client = SandboxClient(SandboxDirectConnectionConfig(api_url="http://10.100.102.150:80"))

sandbox = client.create_sandbox(
    warmpool="bow-sandbox-pool",
    namespace="default",
)
try:
    result = sandbox.commands.run("echo 'Hello from Agent Sandbox!'")
    print(result.stdout)
    result = sandbox.commands.run("date")
    print(result.stdout)
    result = sandbox.commands.run("hostname")
    print(result.stdout)
    # Hello from Agent Sandbox!
finally:
    sandbox.terminate()
