"""Generates a dev Ed25519 key pair for gateway->backend tokens and appends it to .env.

The private key goes to the GATEWAY only; backends get the public key. Values are never printed.
In AWS these live in Secrets Manager. Refuses to overwrite existing values.
"""

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ENV = Path(".env")


PUBLIC_VARS = (
    "FOOTBALL_INTERNAL_PUBLIC_KEY_B64",
    "USER_INTERNAL_PUBLIC_KEY_B64",
    "KNOWLEDGE_INTERNAL_PUBLIC_KEY_B64",
)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def main() -> None:
    existing = ENV.read_text() if ENV.exists() else ""
    if "GATEWAY_INTERNAL_PRIVATE_KEY_B64=" in existing:
        # Keep the existing key pair; just make sure every backend has its public-key variable.
        line = next(
            x for x in existing.splitlines() if x.startswith("GATEWAY_INTERNAL_PRIVATE_KEY_B64=")
        )
        key = serialization.load_pem_private_key(
            base64.b64decode(line.split("=", 1)[1]), password=None
        )
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit("GATEWAY_INTERNAL_PRIVATE_KEY_B64 is not an Ed25519 key")
        new_pair = False
    else:
        key = Ed25519PrivateKey.generate()
        new_pair = True
    public = _b64(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    additions = []
    if new_pair:
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        additions.append(f"GATEWAY_INTERNAL_PRIVATE_KEY_B64={_b64(private)}")
    additions += [f"{v}={public}" for v in PUBLIC_VARS if f"{v}=" not in existing]
    if not additions:
        print("all internal key variables already present; nothing to do")
        return
    with ENV.open("a") as f:
        f.write("\n# --- internal gateway->backend token keys (dev; generated) ---\n")
        f.write("\n".join(additions) + "\n")
    ENV.chmod(0o600)
    print("added to .env:", ", ".join(a.split("=", 1)[0] for a in additions))


if __name__ == "__main__":
    main()
