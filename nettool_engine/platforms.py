"""Work out what kind of system a device is from what the vault says about it.

The platform decides which command guide applies now, and which management
panel and monitoring method will apply later, so it is settled in one place.
"""

from __future__ import annotations

CISCO_IOS = "cisco_ios"
MIKROTIK = "mikrotik"
LINUX = "linux"
WINDOWS = "windows"
UNKNOWN = "unknown"

# First match wins, so the more specific words come first.
_KEYWORDS = (
    (MIKROTIK, ("mikrotik", "routeros", "routerboard", "chr ", "hap ", "ccr", "crs")),
    (CISCO_IOS, ("cisco", "catalyst", "ios-xe", "ios xe", " ios", "nexus")),
    (WINDOWS, ("windows", "win server", "winserver", "microsoft")),
    (LINUX, ("linux", "ubuntu", "debian", "centos", "rhel", "rocky", "alma",
             "fedora", "proxmox", "raspberry")),
)


def from_ssh_banner(banner: str) -> str:
    """The platform an SSH server's greeting gives away, or UNKNOWN.

    RouterOS and IOS run their own SSH servers and say so.  OpenSSH says
    "for_Windows" on Windows; anywhere else it is almost always Linux.
    """
    text = banner.lower()
    if "rosssh" in text or "mikrotik" in text:
        return MIKROTIK
    if "cisco" in text:
        return CISCO_IOS
    if "for_windows" in text:
        return WINDOWS
    if "openssh" in text or "dropbear" in text:
        return LINUX
    return UNKNOWN


def detect(device: dict) -> str:
    fields = device.get("fields") or {}
    text = " ".join(
        str(part) for part in (
            fields.get("vendor"), fields.get("model"), fields.get("device_type"),
            fields.get("firmware"), device.get("title"), *(device.get("tags") or []),
        ) if part
    ).lower()
    # Padded so " ios" matches at the start and "chr " at the end.
    text = f" {text} "
    for platform, keywords in _KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return platform
    # An SSH key entry that says nothing else about itself is, far more often
    # than not, a Linux server.  Naming a vendor on the entry overrides this.
    if device.get("category") == "ssh_key":
        return LINUX
    return UNKNOWN
