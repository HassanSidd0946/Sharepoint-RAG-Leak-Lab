"""The redaction scanner: finds anything in a bundle that identifies the test tenant.

A bundle is meant to be published, so it must hold no tenant or object ids (GUIDs, drive item ids,
drive ids), no UPNs or emails ('@'), no URLs, and no tenant host names. The exporter never copies
such fields (it picks fields one by one); this scanner is the second, independent line: it walks
every key and value of the finished bundle and the export fails if a pattern matches.
"""

import re
from typing import Any

PATTERNS: dict[str, re.Pattern[str]] = {
    "guid": re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    ),
    "at-sign (email or UPN)": re.compile(r"@"),
    "sharepoint.com": re.compile(r"sharepoint\.com", re.IGNORECASE),
    "onmicrosoft": re.compile(r"onmicrosoft", re.IGNORECASE),
    "url": re.compile(r"https?://|www\.", re.IGNORECASE),
    "drive item id": re.compile(r"\b01[A-Z0-9]{30,36}\b"),
    "drive id": re.compile(r"\bb![A-Za-z0-9_\-]{20,}"),
}


class RedactionError(Exception):
    """A bundle contains something that identifies the tenant: it must not be written."""


def find_leaks(value: Any, path: str = "$") -> list[str]:
    """Every place in `value` (any JSON-like object) where a pattern matches, as 'path: pattern: text'."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += find_leaks(str(key), f"{path}.<key>")
            found += find_leaks(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found += find_leaks(item, f"{path}[{i}]")
    elif isinstance(value, str):
        for name, pattern in PATTERNS.items():
            match = pattern.search(value)
            if match:
                found.append(
                    f"{path}: {name}: {value[max(0, match.start() - 20) : match.end() + 20]!r}"
                )
    return found


def assert_clean(bundle: Any, what: str = "bundle") -> None:
    leaks = find_leaks(bundle)
    if leaks:
        raise RedactionError(f"{what} is not safe to publish:\n  " + "\n  ".join(leaks[:10]))
