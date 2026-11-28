"""The docker-ssh driver's SSH layer, against a real SSH server in-process.

An ``asyncssh`` server on localhost stands in for a Linux server: it has a
host key, accepts one password and whatever public keys were installed, and
answers the handful of commands the layer sends. So the protocol-level
promises are checked for real: the host key is pinned before anything is
sent, a changed key is refused, and a one-time password installs BoW's key
and is used for that one login only.
"""
import asyncio
import logging

import asyncssh
import pytest
import pytest_asyncio

from app.services.sandbox.drivers.base import ComputeContext, SandboxDriverError
from app.services.sandbox.drivers.docker_ssh.driver import DockerSSHDriver
from app.services.sandbox.drivers.docker_ssh.ssh import (
    MODE_ROOT, MODE_SUDO, SSHError, SSHHost, fingerprint_of, generate_keypair,
)

pytestmark = pytest.mark.asyncio

PASSWORD = "one-time-Pa55word!"


class _Server:
    """What the fake Linux server remembers."""

    def __init__(self, *, uid="1000", sudo=True, password_login=True):
        self.uid, self.sudo, self.password_login = uid, sudo, password_login
        self.authorized_keys = ""
        self.password_logins = 0
        self.key_logins = 0
        self.commands = []
        self.files = {}
        self.port = 0
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self._listener = None

    @property
    def host_public_key(self):
        return self.host_key.export_public_key("openssh").decode().strip()

    async def start(self):
        outer = self

        class Auth(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return outer.password_login

            def validate_password(self, username, password):
                ok = password == PASSWORD
                outer.password_logins += ok
                return ok

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                ok = key.export_public_key("openssh").decode().split()[1] in outer.authorized_keys
                outer.key_logins += ok
                return ok

        async def handle(process):
            command = process.command or ""
            outer.commands.append(command)
            status, out, err = 0, "", ""
            bare = command.replace("sudo -n -- ", "")
            if command == "id -u":
                out = outer.uid + "\n"
            elif command == "sudo -n true":
                status, err = (0, "") if outer.sudo else (1, "sudo: a password is required\n")
            elif command.startswith("cat ~/.ssh/authorized_keys"):
                out = outer.authorized_keys
            elif "cat >> ~/.ssh/authorized_keys" in command:
                outer.authorized_keys += await process.stdin.read()
            elif bare.startswith("install -D -m"):
                outer.files[bare.split()[-1]] = await process.stdin.read()
            elif bare.startswith("cat "):
                path = bare.split()[-1]
                status, out = (0, outer.files[path]) if path in outer.files else (1, "")
            elif bare == "false":
                status, err = 3, "it failed\n"
            elif bare.startswith("sleep"):
                await asyncio.sleep(30)
            else:
                out = f"ran: {bare}\n"
            process.stdout.write(out)
            process.stderr.write(err)
            process.exit(status)

        self._listener = await asyncssh.listen(
            "127.0.0.1", 0, server_factory=Auth, server_host_keys=[self.host_key], process_factory=handle,
        )
        self.port = self._listener.get_port()
        return self

    async def stop(self):
        self._listener.close()
        await self._listener.wait_closed()


@pytest_asyncio.fixture
async def server():
    instance = await _Server().start()
    yield instance
    await instance.stop()


@pytest.fixture
def keypair():
    return generate_keypair()


def _host(server, private_key, pinned=True):
    return SSHHost("127.0.0.1", server.port, "ubuntu", private_key,
                   pinned_host_key=server.host_public_key if pinned else None)


async def test_the_host_key_can_be_read_without_logging_in(server, keypair):
    host = _host(server, keypair[0], pinned=False)
    assert await host.fetch_host_key() == server.host_public_key
    assert server.key_logins == 0 and server.password_logins == 0 and server.commands == []


async def test_nothing_is_sent_to_a_server_whose_host_key_is_not_confirmed(server, keypair):
    server.authorized_keys = keypair[1]
    host = _host(server, keypair[0], pinned=False)
    with pytest.raises(SSHError) as refused:
        await host.run(["docker", "info"])
    assert refused.value.code == "host_key_unconfirmed"
    assert server.key_logins == 0 and server.commands == []


async def test_a_server_presenting_another_host_key_is_refused(server, keypair):
    server.authorized_keys = keypair[1]
    imposter_key = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode().strip()
    host = SSHHost("127.0.0.1", server.port, "ubuntu", keypair[0], pinned_host_key=imposter_key)
    with pytest.raises(SSHError) as refused:
        await host.run(["docker", "info"])
    assert refused.value.code == "host_key_mismatch"
    assert server.key_logins == 0 and server.commands == [], "no login and no command reached the wrong server"


async def test_commands_run_through_sudo_for_a_non_root_user_and_plainly_for_root(server, keypair):
    server.authorized_keys = keypair[1]
    host = _host(server, keypair[0])
    try:
        assert await host.detect_mode() == MODE_SUDO
        result = await host.run(["docker", "run", "an image; id"])
        assert result.stdout == "ran: docker run 'an image; id'\n"
        assert server.commands[-1] == "sudo -n -- docker run 'an image; id'"
    finally:
        await host.close()

    server.uid = "0"
    root = _host(server, keypair[0])
    try:
        assert await root.detect_mode() == MODE_ROOT
        await root.run(["docker", "ps"])
        assert server.commands[-1] == "docker ps"
    finally:
        await root.close()


async def test_a_user_who_would_be_prompted_for_a_sudo_password_is_refused(server, keypair):
    server.authorized_keys, server.sudo = keypair[1], False
    host = _host(server, keypair[0])
    try:
        with pytest.raises(SSHError) as refused:
            await host.run(["docker", "ps"])
        assert refused.value.code == "no_privileges"
        assert not any(c.startswith("sudo -n -- docker") for c in server.commands), "the command itself never ran"
    finally:
        await host.close()


async def test_a_failing_command_reports_its_exit_status_and_a_slow_one_times_out(server, keypair):
    server.authorized_keys = keypair[1]
    host = _host(server, keypair[0])
    try:
        with pytest.raises(SSHError) as failed:
            await host.run(["false"])
        assert failed.value.code == "command_failed" and "exit 3" in failed.value.message and "it failed" in failed.value.message
        assert (await host.run(["false"], check=False)).exit_status == 3
        with pytest.raises(SSHError) as slow:
            await host.run(["sleep", "30"], timeout=0.5)
        assert slow.value.code == "timeout"
    finally:
        await host.close()


async def test_files_are_written_through_standard_input(server, keypair):
    server.authorized_keys = keypair[1]
    host = _host(server, keypair[0])
    try:
        await host.write_file("/var/lib/bow-sandbox/env/x.env", "API_KEY=s3cr3t\n")
        assert server.files["/var/lib/bow-sandbox/env/x.env"] == "API_KEY=s3cr3t\n"
        assert not any("s3cr3t" in command for command in server.commands)
        assert await host.read_file("/var/lib/bow-sandbox/env/x.env") == "API_KEY=s3cr3t\n"
        assert await host.read_file("/nope") is None
    finally:
        await host.close()


async def test_bows_key_is_refused_until_it_is_installed(server, keypair):
    host = _host(server, keypair[0])
    with pytest.raises(SSHError) as refused:
        await host.run(["docker", "ps"])
    assert refused.value.code == "auth_failed" and "ubuntu" in refused.value.message


async def test_a_one_time_password_installs_the_key_and_is_used_for_one_login_only(server, keypair, caplog):
    caplog.set_level(logging.DEBUG)
    private_key, public_key = keypair
    host = _host(server, private_key)
    try:
        await host.install_public_key(PASSWORD, public_key)
        assert public_key.split()[1] in server.authorized_keys
        assert server.password_logins == 1 and server.key_logins >= 1, "it finished by proving key login works"
        await host.run(["docker", "ps"])
        assert server.password_logins == 1, "every later connection uses the key"
        # Installing again does not add the key twice.
        await host.install_public_key(PASSWORD, public_key)
        assert server.authorized_keys.count(public_key.split()[1]) == 1
    finally:
        await host.close()
    assert PASSWORD not in " ".join(server.commands)
    assert PASSWORD not in caplog.text


async def test_a_wrong_one_time_password_is_refused_and_installs_nothing(server, keypair):
    host = _host(server, keypair[0])
    with pytest.raises(SSHError) as refused:
        await host.install_public_key("not-the-password", keypair[1])
    assert refused.value.code == "auth_failed" and "one-time password" in refused.value.message
    assert server.authorized_keys == ""


# ── through the driver ───────────────────────────────────────────────────


def _driver(server, keypair, **state):
    ctx = ComputeContext(
        compute_id="b", name="b", instance_id="i",
        config={"address": "127.0.0.1", "ssh_port": server.port, "ssh_user": "ubuntu", "ssh_public_key": keypair[1]},
        credentials={"ssh_private_key": keypair[0]}, driver_state=dict(state),
    )
    return DockerSSHDriver(ctx)


async def test_test_connection_shows_the_fingerprint_until_it_is_confirmed_then_pins_it(server, keypair):
    server.authorized_keys = keypair[1]
    driver = _driver(server, keypair)
    try:
        first = await driver.check()
        fingerprint = fingerprint_of(server.host_public_key)
        assert first.reachable and not first.prepared
        assert first.checks[0]["status"] == "warning" and fingerprint in first.checks[0]["detail"]
        assert "ssh_host_key" not in driver.ctx.driver_state and server.key_logins == 0, "not logged in yet"

        with pytest.raises(SandboxDriverError) as wrong:
            await driver.confirm_identity("SHA256:" + "A" * 43)
        assert wrong.value.code == "host_key_mismatch" and "ssh_host_key" not in driver.ctx.driver_state

        await driver.confirm_identity(fingerprint)
        assert driver.ctx.driver_state["ssh_host_key"] == server.host_public_key
        assert DockerSSHDriver.public_info(driver.ctx.driver_state)["host_key_fingerprint"] == fingerprint
        second = await driver.check()
        assert second.checks[0] == {"key": "host_key", "title": "SSH host key", "status": "ok", "detail": f"Pinned: {fingerprint}"}
        assert server.key_logins >= 1
    finally:
        await driver.close()


async def test_a_fingerprint_given_with_the_compute_is_pinned_on_first_contact_only_if_it_matches(server, keypair):
    server.authorized_keys = keypair[1]
    matching = _driver(server, keypair)
    matching.config.ssh_host_key_fingerprint = fingerprint_of(server.host_public_key)
    wrong = _driver(server, keypair)
    wrong.config.ssh_host_key_fingerprint = "SHA256:" + "B" * 43
    try:
        await matching.check()
        assert matching.ctx.driver_state["ssh_host_key"] == server.host_public_key
        await wrong.check()
        assert "ssh_host_key" not in wrong.ctx.driver_state
    finally:
        await matching.close()
        await wrong.close()


async def test_bootstrap_refuses_a_server_whose_host_key_is_not_confirmed(server, keypair):
    driver = _driver(server, keypair)
    try:
        steps = await driver.prepare()
    finally:
        await driver.close()
    assert steps[0].status == "failed" and "fingerprint" in steps[0].detail
    assert all(step.status == "skipped" for step in steps[1:])
    assert server.commands == []


async def test_a_one_time_password_is_only_sent_to_a_confirmed_server(server, keypair):
    unconfirmed = _driver(server, keypair)
    try:
        with pytest.raises(SandboxDriverError) as refused:
            await unconfirmed.enroll({"one_time_password": PASSWORD})
        assert refused.value.code == "host_key_unconfirmed" and server.password_logins == 0
    finally:
        await unconfirmed.close()

    confirmed = _driver(server, keypair, ssh_host_key=server.host_public_key)
    try:
        await confirmed.enroll({"one_time_password": PASSWORD})
        assert server.password_logins == 1 and keypair[1].split()[1] in server.authorized_keys
        assert PASSWORD not in str(confirmed.ctx.driver_state) and PASSWORD not in str(confirmed.ctx.credentials)
        assert (await confirmed.check()).checks[1]["status"] == "ok"
    finally:
        await confirmed.close()
