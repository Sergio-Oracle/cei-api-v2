#!/usr/bin/env python3
"""
Génère un keypair Ed25519 pour PASETO v4.public.
pyseto v1.9.3 attend des clés au format PEM encodées en base64.
Exécuter UNE SEULE FOIS par serveur, puis ajouter les valeurs dans .env

Usage : python scripts/generate_paseto_keys.py
"""
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, PublicFormat, NoEncryption
)

priv = Ed25519PrivateKey.generate()
pub  = priv.public_key()

priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
pub_pem  = pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

# Encoder en base64 pour une seule ligne dans .env
priv_b64 = base64.b64encode(priv_pem).decode()
pub_b64  = base64.b64encode(pub_pem).decode()

print("# ── PASETO v4 Ed25519 Keys (PEM en base64) ──")
print(f"PASETO_PRIVATE_KEY={priv_b64}")
print(f"PASETO_PUBLIC_KEY={pub_b64}")
print(f"PASETO_ACCESS_TTL_MIN=15")
print(f"PASETO_REFRESH_TTL_DAYS=7")
print()
print("# ⚠️  Ne jamais commiter PASETO_PRIVATE_KEY dans git !")
