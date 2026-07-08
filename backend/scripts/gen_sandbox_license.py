"""Generate a test enterprise license for the seat-cap sandbox feedback loop and
install its public key so a locally-run server trusts it.

FOR LOCAL SANDBOX VERIFICATION ONLY — this mints a throwaway keypair, it does not
touch or reveal the real signing key. It backs up the tracked public-key pem to
``<pem>.orig`` first; restore it when done:

    mv app/ee/license_public_key.pem.orig app/ee/license_public_key.pem

Usage (run from the backend/ dir):
    LIC=$(python scripts/gen_sandbox_license.py 3)
    BOW_LICENSE_KEY="$LIC" BOW_DATABASE_URL=sqlite:///db/app_sandbox.db python main.py

Prints the signed license key (bow_lic_...) to stdout; progress goes to stderr.
"""
import os
import sys
import shutil
from datetime import datetime, timezone, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

# Resolve the pem path relative to this file so the script works from any cwd.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PEM_PATH = os.path.join(_BACKEND_DIR, "app", "ee", "license_public_key.pem")


def main():
    max_users = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    # Back up and install the test public key so the server verifies our license.
    if not os.path.exists(PEM_PATH + ".orig"):
        shutil.copyfile(PEM_PATH, PEM_PATH + ".orig")
    with open(PEM_PATH, "w") as f:
        f.write(public_pem)

    now = datetime.now(timezone.utc)
    payload = {
        "iss": "bagofwords.com",
        "sub": "lic_sandbox_seatcap",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=365)).timestamp()),
        "tier": "enterprise",           # tier defaults include scim + domain_signup
        "org_name": "Seat Cap Sandbox",
        "max_users": max_users,         # the cap under test
    }
    token = jwt.encode(payload, private_pem, algorithm="RS256")
    sys.stderr.write(f"Installed test public key at {PEM_PATH} (backup at {PEM_PATH}.orig)\n")
    sys.stderr.write(f"License: tier=enterprise max_users={max_users}\n")
    sys.stderr.write("Restore with: mv app/ee/license_public_key.pem.orig app/ee/license_public_key.pem\n")
    print(f"bow_lic_{token}")


if __name__ == "__main__":
    main()
