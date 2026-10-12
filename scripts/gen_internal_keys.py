"""Generates a dev Ed25519 key pair for gateway->backend tokens and appends it to .env.

The private key goes to the GATEWAY only; backends get the public key. Values are never printed.
In AWS these live in Secrets Manager. Refuses to overwrite existing values.
"""

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ENV = Path(".env")


def main() -> None:
    existing = ENV.read_text() if ENV.exists() else ""
    if "GATEWAY_INTERNAL_PRIVATE_KEY_B64=" in existing:
        print("keys already present in .env; not overwriting")
        return
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    with ENV.open("a") as f:
        f.write("\n# --- internal gateway->backend token keys (dev; generated) ---\n")
        f.write(f"GATEWAY_INTERNAL_PRIVATE_KEY_B64={b64(private)}\n")
        f.write(f"FOOTBALL_INTERNAL_PUBLIC_KEY_B64={b64(public)}\n")
    ENV.chmod(0o600)
    print("wrote GATEWAY_INTERNAL_PRIVATE_KEY_B64 and FOOTBALL_INTERNAL_PUBLIC_KEY_B64 to .env")


if __name__ == "__main__":
    main()
