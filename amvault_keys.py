"""Identify which attachment on an entry is the private SSH key.

Detection reads file headers rather than filenames: a key saved as
`github_backup` is still a private key, and one named `id_rsa.pub` is still
public. Shared by the CLI and the GUI so both behave identically.
"""

from __future__ import annotations

# Every private key format OpenSSH accepts announces itself on the first line.
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
)

# A public key is a single line starting with its algorithm name.
PUBLIC_KEY_PREFIXES = (
    b"ssh-rsa", b"ssh-ed25519", b"ssh-dss",
    b"ecdsa-sha2-", b"sk-ssh-ed25519", b"sk-ecdsa-sha2-",
)


def looks_like_private_key(data: bytes) -> bool:
    head = data.lstrip()[:200]
    return any(head.startswith(marker) for marker in PRIVATE_KEY_MARKERS)


def looks_like_public_key(data: bytes) -> bool:
    head = data.lstrip()[:64]
    return any(head.startswith(prefix) for prefix in PUBLIC_KEY_PREFIXES)


def private_keys(entry) -> list:
    """Every attachment that is a private key."""
    return [a for a in entry.attachments if looks_like_private_key(a.data)]


def pick_private_key(entry, wanted: str | None = None):
    """The single private key on an entry, or None if that is ambiguous.

    Returns None when there is no key or more than one, leaving the caller to
    decide how to report it -- the CLI prints a list, the GUI shows a dialog.
    """
    if wanted:
        match = next((a for a in entry.attachments if a.name == wanted), None)
        if match is None or not looks_like_private_key(match.data):
            return None
        return match

    keys = private_keys(entry)
    return keys[0] if len(keys) == 1 else None
