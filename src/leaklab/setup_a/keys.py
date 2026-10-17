"""The key of an indexed document, derived from Graph ids (so a document can be looked up by key).

Observed 2026-10-17 (probes/search_acl_probe.py, stage lookup): the SharePoint indexer with
`base64Encode` on `metadata_spo_site_library_item_id` stores keys that decode to
    "<site id>::<drive id>::<drive item id>"
for example "leaklabdev.sharepoint.com,<site collection guid>,<web guid>::b!<drive>::01<item>".
The encoding is URL-safe base64. EMPIRICAL, not documented: the item part the INDEXER stores is
Graph's driveItem id plus one extra character "5" (6 of 6 documents compared on 2026-10-17: 34 -> 35
characters, always "5"). The probe `lookup` checks derived keys against the index; if a lookup by a
derived key ever misses a document the index holds, this derivation is wrong.
"""

import base64

INDEXER_ITEM_SUFFIX = "5"  # observed on every compared document, see the module docstring


def doc_key(site_id: str, drive_id: str, item_id: str) -> str:
    """The index key of a library document (URL-safe base64 of "site::drive::item<suffix>")."""
    raw = f"{site_id}::{drive_id}::{item_id}{INDEXER_ITEM_SUFFIX}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def parse_key(key: str) -> tuple[str, str, str]:
    """The (site id, drive id, item id) a key encodes; ValueError if it has another shape."""
    padded = key + "=" * (-len(key) % 4)
    text = base64.urlsafe_b64decode(padded).decode("utf-8")
    parts = text.split("::")
    if len(parts) != 3:
        raise ValueError(f"not a SharePoint library document key: {text[:80]!r}")
    return parts[0], parts[1], parts[2]


def item_id_of_key(key: str) -> str:
    """The Graph drive item id behind an index key: the key's item part without the indexer's
    extra suffix. ValueError if the key has another shape (the caller must then drop the hit)."""
    item = parse_key(key)[2]
    if not item.endswith(INDEXER_ITEM_SUFFIX) or len(item) < 2:
        raise ValueError("item part does not end with the indexer suffix")
    return item[: -len(INDEXER_ITEM_SUFFIX)]
