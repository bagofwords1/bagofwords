# Sandbox driver contract suite

One set of tests that every sandbox driver must pass. A driver is done when
it passes this suite; nothing above the driver may depend on behaviour the
suite does not pin down.

| File | What it is |
|---|---|
| `contract.py` | The tests. They only use the `SandboxDriver` interface, the runtime client, and a `harness` fixture. Not collected on its own |
| `harness.py` | `DriverHarness`: what a driver's test module supplies (a prepared driver, how to probe the network from inside a sandbox, how to plant an orphan) |
| `tests/unit/test_sandbox_driver_contract_fake.py` | Runs the suite against the **fake** driver. Part of the default unit run, so it runs in CI |
| `test_docker_ssh_live.py` | Runs the suite against a real Docker host. Skipped unless `BOW_SANDBOX_TEST_HOST` is set; run from the live feedback loop |

To add a driver: write a module that defines a `harness` fixture returning a
`DriverHarness` for it and does `from tests.contracts.sandbox_driver.contract import *`.
