"""Run PowerShell on a Windows machine over SSH and get JSON back.

Windows' SSH server hands commands to cmd.exe (or to PowerShell, if the admin
changed the default shell), and each has its own quoting rules.  Sending the
script base64-encoded sidesteps both: there is nothing left to quote.
"""

from __future__ import annotations

import base64
import json

# Progress records would otherwise arrive as XML mixed into the output, and
# the console's legacy code page would mangle non-Latin names.
_PREAMBLE = (
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
)


def command(script: str) -> str:
    """The command line that runs `script` in Windows PowerShell."""
    encoded = base64.b64encode((_PREAMBLE + script).encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"


def decode(command_line: str) -> str:
    """The script inside a command made by `command()` -- for tests and for
    showing a person what will run."""
    encoded = command_line.rsplit(" ", 1)[-1]
    return base64.b64decode(encoded).decode("utf-16-le")[len(_PREAMBLE):]


def quote(value: str) -> str:
    """A PowerShell single-quoted string, in which only ' is special."""
    return "'" + value.replace("'", "''") + "'"


def parse_json(text: str):
    """The JSON value in a command's output, tolerating a banner around it."""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[min(starts):])[0]
    except ValueError:
        return None


def rows(text: str) -> list[dict]:
    """A JSON array of objects; a lone object counts as a list of one."""
    value = parse_json(text)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []
