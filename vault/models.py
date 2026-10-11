"""Data model for vault entries.

An entry has a small set of always-present fields (title, category, tags,
notes) plus a free-form `fields` dict whose meaning depends on the category.
That keeps one storage format flexible enough for logins, SSH keys, API
tokens and network gear without a schema migration each time a new kind of
secret shows up.

Which fields count as *secret* is declared per category in SECRET_FIELDS.  The
GUI masks those by default and the CLI refuses to print them without an
explicit flag.
"""

from __future__ import annotations

import base64
import secrets
import time
from dataclasses import dataclass, field


class Category:
    LOGIN = "login"
    SSH_KEY = "ssh_key"
    API_KEY = "api_key"
    NETWORK = "network"
    NOTE = "note"

    ALL = [LOGIN, SSH_KEY, API_KEY, NETWORK, NOTE]

    LABELS = {
        LOGIN: "Login",
        SSH_KEY: "SSH Key / Certificate",
        API_KEY: "API Key / Token",
        NETWORK: "Network Device",
        NOTE: "Secure Note",
    }


# Field names that must never be shown in plain text without an explicit
# reveal.  Anything not listed here is treated as metadata.
SECRET_FIELDS = {
    Category.LOGIN: {"password", "totp_secret"},
    Category.SSH_KEY: {"passphrase", "private_key"},
    Category.API_KEY: {"key", "secret"},
    Category.NETWORK: {"password", "enable_password", "snmp_community", "wifi_psk"},
    Category.NOTE: {"content"},
}

# Suggested fields per category. The UI offers these; users can add their own.
TEMPLATE_FIELDS = {
    Category.LOGIN: ["username", "password", "url", "totp_secret"],
    Category.SSH_KEY: ["username", "host", "port", "key_type", "passphrase", "fingerprint"],
    Category.API_KEY: ["service", "key", "secret", "endpoint", "expires"],
    Category.NETWORK: [
        "device_type", "vendor", "model", "mgmt_ip", "mgmt_port",
        "username", "password", "enable_password", "snmp_community",
        "wifi_ssid", "wifi_psk", "location", "firmware", "rustdesk_id",
    ],
    Category.NOTE: ["content"],
}


def _now() -> float:
    return time.time()


def new_id() -> str:
    return secrets.token_hex(8)


@dataclass
class Attachment:
    """A file stored inside an entry, e.g. an SSH private key or a cert.

    The bytes live base64-encoded inside the entry, which is itself encrypted
    as a whole, so attachments get exactly the same protection as passwords.
    """

    name: str
    data: bytes
    added: float = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "data": base64.b64encode(self.data).decode("ascii"),
            "added": self.added,
        }

    @staticmethod
    def from_dict(d: dict) -> "Attachment":
        return Attachment(
            name=d["name"],
            data=base64.b64decode(d["data"]),
            added=d.get("added", 0.0),
        )

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class Entry:
    title: str
    category: str = Category.LOGIN
    fields: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    notes: str = ""
    attachments: list = field(default_factory=list)
    id: str = field(default_factory=new_id)
    created: float = field(default_factory=_now)
    modified: float = field(default_factory=_now)

    def secret_field_names(self) -> set:
        return SECRET_FIELDS.get(self.category, set())

    def is_secret_field(self, name: str) -> bool:
        return name in self.secret_field_names()

    def touch(self) -> None:
        self.modified = _now()

    def matches(self, query: str) -> bool:
        """Case-insensitive search across non-secret text.

        Secret values are excluded on purpose: matching against them would let
        someone with screen access confirm a guessed password by watching
        which entries filter in.
        """
        if not query:
            return True
        q = query.lower()
        haystack = [self.title, self.notes, self.category, *self.tags]
        haystack += [a.name for a in self.attachments]
        haystack += [
            f"{k} {v}" for k, v in self.fields.items() if not self.is_secret_field(k)
        ]
        return any(q in str(h).lower() for h in haystack)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "fields": self.fields,
            "tags": self.tags,
            "notes": self.notes,
            "attachments": [a.to_dict() for a in self.attachments],
            "created": self.created,
            "modified": self.modified,
        }

    @staticmethod
    def from_dict(d: dict) -> "Entry":
        return Entry(
            id=d.get("id", new_id()),
            title=d.get("title", ""),
            category=d.get("category", Category.LOGIN),
            fields=dict(d.get("fields", {})),
            tags=list(d.get("tags", [])),
            notes=d.get("notes", ""),
            attachments=[Attachment.from_dict(a) for a in d.get("attachments", [])],
            created=d.get("created", _now()),
            modified=d.get("modified", _now()),
        )
