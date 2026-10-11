#!/usr/bin/env python3
"""Answer ssh's password question from the vault -- once.

ssh runs the program named in SSH_ASKPASS whenever it needs something typed,
passing the question as its argument and reading the answer from our stdout.
amvault points SSH_ASKPASS here when it opens a session for an entry that has
a password, so the session signs in without the password being typed, pasted,
or placed anywhere another program could find it.

Only the first password question is answered that way.  Everything else ssh
asks -- whether to trust a new host key, a key's passphrase, the password
again after a refusal -- is put to the person in the terminal, exactly as ssh
would have done itself.  In particular a host key is never accepted on the
person's behalf.
"""

from __future__ import annotations

import getpass
import os
import sys


def from_vault() -> str | None:
    """The password amvault is holding for this session, or None if it has
    already been handed over or the offer has expired."""
    address = os.environ.get("AMVAULT_ASKPASS_ADDRESS")
    token = os.environ.get("AMVAULT_ASKPASS_TOKEN")
    if not address or not token:
        return None
    try:
        from multiprocessing.connection import Client

        family = os.environ.get("AMVAULT_ASKPASS_FAMILY") or "AF_UNIX"
        with Client(address, family=family, authkey=bytes.fromhex(token)) as connection:
            return connection.recv_bytes(4096).decode("utf-8")
    except Exception:
        return None


def ask_person(prompt: str) -> str:
    """Put ssh's question to whoever is at the terminal."""
    if "yes/no" in prompt.lower():
        # A host key question: the answer is not a secret, so let it be seen.
        if os.name == "nt":
            return input(prompt)
        # Two handles, not one opened "r+": Python refuses to open a
        # terminal for buffered read-and-write, since it cannot seek.
        with open("/dev/tty", "w", encoding="utf-8", errors="replace") as out:
            out.write(prompt)
            out.flush()
        with open("/dev/tty", "r", encoding="utf-8", errors="replace") as source:
            return source.readline().rstrip("\n")
    return getpass.getpass(prompt)


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Password: "
    answer = None
    lowered = prompt.lower()
    if "password" in lowered and "yes/no" not in lowered:
        answer = from_vault()
    if answer is None:
        try:
            answer = ask_person(prompt)
        except (EOFError, KeyboardInterrupt, OSError):
            return 1
    sys.stdout.write(answer + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
