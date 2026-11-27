"""The ``docker-ssh`` sandbox driver: one Linux server with Docker, reached over SSH.

Everything specific to this kind of backend lives in this package and nowhere
else: the SSH layer, the Docker commands, the Caddy router, the egress proxy
and the firewall helper. Nothing above the driver interface imports from here
except the driver registry, and that only by dotted path, lazily.

    schemas.py    the backend form: config and credentials
    layout.py     names, paths and addresses on a host
    ssh.py        SSH layer: key auth, pinned host key, root or `sudo -n`
    commands.py   fixed command templates and the configs they send (pure)
    host.py       operations on a host built from those commands
    bootstrap.py  the steps that prepare a host
    driver.py     the SandboxDriver implementation
"""
