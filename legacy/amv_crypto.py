"""Crypto primitives for the vault.

Design notes
------------
Key derivation is Argon2id (memory-hard, resists GPU/ASIC cracking).  The
derived value is *not* a password hash we compare against -- it *is* the
encryption key.  There is no stored "correct password" to check, so the only
way to know a password is right is that decryption authenticates.  That is
deliberate: it means an attacker with the file gets no verification oracle
cheaper than a full Argon2id pass.

Two-factor unlocking is done by mixing an optional key file into the Argon2id
password input.  A key file participates in the *key* itself, so a missing key
file makes the vault mathematically undecryptable -- unlike a TOTP prompt,
which is only a software check an attacker can patch out.

Content encryption is AES-256-GCM: authenticated, so tampering with the
ciphertext is detected rather than silently producing garbage.
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# --- Argon2id parameters -------------------------------------------------
# Tuned for an interactive desktop unlock: ~0.5-1s on a modern laptop.
# Stored per-vault in the header so old files stay readable if we raise these.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 256 * 1024  # KiB -> 256 MiB
ARGON2_PARALLELISM = 4

# Floors we refuse to go below when *reading* a vault.  Parameters live in the
# cleartext header; although GCM authentication would catch an edit, the KDF
# runs before that check, so an attacker-supplied header could otherwise make
# us derive a key with trivially weak settings (or crash argon2 outright).
# We validate first and fail loudly instead.
MIN_TIME_COST = 2
MIN_MEMORY_COST = 8 * 1024  # KiB -> 8 MiB
MIN_PARALLELISM = 1
MIN_SALT_LEN = 8

KEY_LEN = 32  # AES-256
SALT_LEN = 16
NONCE_LEN = 12  # GCM standard nonce size


class WeakParametersError(Exception):
    """Raised when a vault header asks for KDF settings below our floor."""


class DecryptionError(Exception):
    """Raised when a ciphertext fails to authenticate.

    This means one of: wrong master password, wrong/missing key file, or the
    data was corrupted or tampered with.  We deliberately do not distinguish
    between these, since telling an attacker *which* factor was wrong leaks
    information.
    """


@dataclass(frozen=True)
class KdfParams:
    """Argon2id parameters, persisted alongside the vault."""

    salt: bytes
    time_cost: int = ARGON2_TIME_COST
    memory_cost: int = ARGON2_MEMORY_COST
    parallelism: int = ARGON2_PARALLELISM

    def validate(self) -> None:
        """Reject settings weak enough to make brute force cheap.

        Called on load, before the key is derived.
        """
        if len(self.salt) < MIN_SALT_LEN:
            raise WeakParametersError(
                f"Vault salt is {len(self.salt)} bytes; the minimum is {MIN_SALT_LEN}."
            )
        if self.time_cost < MIN_TIME_COST:
            raise WeakParametersError(
                f"Vault asks for time_cost={self.time_cost}; the minimum is {MIN_TIME_COST}."
            )
        if self.memory_cost < MIN_MEMORY_COST:
            raise WeakParametersError(
                f"Vault asks for memory_cost={self.memory_cost} KiB; the minimum is "
                f"{MIN_MEMORY_COST} KiB. The file may have been tampered with."
            )
        if self.parallelism < MIN_PARALLELISM:
            raise WeakParametersError(
                f"Vault asks for parallelism={self.parallelism}; the minimum is "
                f"{MIN_PARALLELISM}."
            )

    @staticmethod
    def generate() -> "KdfParams":
        return KdfParams(salt=secrets.token_bytes(SALT_LEN))

    def to_dict(self) -> dict:
        return {
            "salt": self.salt.hex(),
            "time_cost": self.time_cost,
            "memory_cost": self.memory_cost,
            "parallelism": self.parallelism,
        }

    @staticmethod
    def from_dict(d: dict) -> "KdfParams":
        try:
            return KdfParams(
                salt=bytes.fromhex(d["salt"]),
                time_cost=int(d["time_cost"]),
                memory_cost=int(d["memory_cost"]),
                parallelism=int(d["parallelism"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeakParametersError(f"Vault header has malformed KDF parameters: {exc}") from exc


def read_key_file(path: str) -> bytes:
    """Load a key file's contents as raw factor material.

    Any file works -- a random blob, a photo, whatever.  We hash whatever bytes
    are there, so the file's format is irrelevant; only its exact contents
    matter.  Change one byte and the vault no longer opens.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if not data:
        raise ValueError("Key file is empty; it would add no security.")
    return data


def generate_key_file(path: str, size: int = 64) -> None:
    """Write a fresh random key file, readable only by the owner."""
    blob = secrets.token_bytes(size)
    # Create with 0600 from the start rather than chmod-ing after, so the
    # bytes are never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)


def derive_key(password: str, params: KdfParams, key_file_bytes: bytes | None = None) -> bytes:
    """Derive the vault's master key from the password and optional key file.

    The key file is appended to the password bytes before hashing, so both
    factors feed the same Argon2id call and both are required to reproduce the
    key.
    """
    secret = password.encode("utf-8")
    if key_file_bytes:
        # Domain-separate the two factors so a password ending in the key
        # file's bytes cannot be confused with the two-factor case.
        secret = secret + b"\x00\xffKEYFILE\xff\x00" + key_file_bytes

    return hash_secret_raw(
        secret=secret,
        salt=params.salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost,
        parallelism=params.parallelism,
        hash_len=KEY_LEN,
        type=Argon2Type.ID,
    )


def encrypt(key: bytes, plaintext: bytes, associated_data: bytes = b"") -> bytes:
    """Encrypt with AES-256-GCM, returning nonce || ciphertext || tag.

    A fresh random nonce per call. With a 96-bit random nonce, reuse becomes a
    realistic worry only after ~2^32 encryptions under one key; a personal
    vault will not approach that, and rekeying on password change resets it.
    """
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, associated_data)
    return nonce + ct


def decrypt(key: bytes, blob: bytes, associated_data: bytes = b"") -> bytes:
    """Reverse of `encrypt`. Raises DecryptionError if authentication fails."""
    if len(blob) < NONCE_LEN + 16:
        raise DecryptionError("Ciphertext is too short to be valid.")
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ct, associated_data)
    except Exception as exc:  # cryptography raises InvalidTag
        raise DecryptionError(
            "Could not decrypt: wrong password, wrong key file, or corrupted data."
        ) from exc


def constant_time_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)
