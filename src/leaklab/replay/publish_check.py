"""Check the publication manifest (docs/PUBLISH.md) against what git tracks, and scan the public files.

  uv run python -m leaklab.replay publish-check

1. Every tracked file is classified: PUBLIC (listed in the manifest), PRIVATE (listed as private) or
   UNLISTED. Tracked files that are private or unlisted would become public with the repository: they are
   reported and the command fails.
2. Every PUBLIC file is scanned for what must not be published: GUIDs (other than well-known Microsoft
   resource ids and obviously fake test ids), user principal names / e-mail addresses on a real domain,
   tenant host names, JWTs, private keys, secret-looking assignments, SharePoint drive and item ids, and, if a
   local .env exists, the exact values in it (tenant name, tenant id, app ids, admin name, search service name,
   passwords, secrets). Findings print the file, the line number and the KIND only, never the value.
"""

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

PUBLIC = [
    "src/*",
    "tests/*",
    "scripts/*",
    "docs/*",
    "optimus-main/*",
    "probes/*.py",
    "tenant_spec.yaml",
    "pyproject.toml",
    "uv.lock",
    "docker-compose.yml",
    ".env.example",
    ".gitignore",
    "LICENSE",
    "README.md",
    "CLAUDE.md",
]
PRIVATE = [
    "runs/*",
    "probes/out/*",
    "certs/*",
    ".env",
    ".env.*",
    ".claude/*",
    "PROMPT_*.md",
    "beam-main/*",
    "pulse-metrics-main/*",
    "DAY0_DAY1_RUNBOOK.md",
    "docs/*HANDOFF.md",
    "optimus-main/node_modules/*",
    "optimus-main/.next/*",
    "*.pem",
    "*.pfx",
    "*.key",
]
PRIVATE_EXCEPTIONS = [".env.example"]
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".ico", ".gif", ".woff", ".woff2", ".pyc", ".lock"}
MICROSOFT_PUBLIC_IDS = {
    "00000003-0000-0ff1-ce00-000000000000",  # SharePoint Online resource id
    "00000003-0000-0000-c000-000000000000",  # Microsoft Graph resource id
    "880da380-985e-4198-81b9-e05b1cc53158",  # Azure AI Search resource app id (Microsoft first-party)
}
FAKE_DOMAINS = {
    "contoso",
    "example",
    "t",
    "x",
    "tenant",
    "fabrikam",
    "yourtenant",
    "svc",
    "s",
    "anthropic",
}
FIXTURE_MARKER = "publish-check: fixture"  # a line with this comment is a scanner test fixture
SCAN_EXEMPT = {
    "tests/test_publish_check.py",
    "tests/test_build_public.py",
}  # the scanner's own fake fixtures
BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

GUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+)(?:\.[A-Za-z0-9-]+)+")
HOST = re.compile(
    r"([A-Za-z0-9-]+)(?:-my)?\.(?:sharepoint\.com|onmicrosoft\.com|search\.windows\.net)", re.I
)
PATTERNS = {
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    "PRIVATE_KEY": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "SECRET_VALUE": re.compile(
        r"(?i)\b(client_secret|secret|api[_-]?key|access_token|bearer)\b\s*[:=]\s*['\"][A-Za-z0-9/+_.~-]{16,}['\"]"
    ),
    "DRIVE_ITEM_ID": re.compile(r"\b01[A-Z0-9]{30,36}\b"),
    "DRIVE_ID": re.compile(r"\bb![A-Za-z0-9_-]{20,}"),
}
FAKE_ITEM_ID = re.compile(r"^01(FAKE)+")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str


def classify(path: str) -> str:
    """PRIVATE, PUBLIC or UNLISTED for a repo-relative path (private wins)."""
    if path not in PRIVATE_EXCEPTIONS and any(fnmatch(path, p) for p in PRIVATE):
        return "PRIVATE"
    if any(fnmatch(path, p) for p in PUBLIC):
        return "PUBLIC"
    return "UNLISTED"


def _fake_guid(g: str) -> bool:
    digits = g.replace("-", "").lower()
    return g.lower() in MICROSOFT_PUBLIC_IDS or len(set(digits)) <= 5


ALLOW_FILE = "docs/publish-allow.txt"


def allowed_values(root: Path) -> list[str]:
    """Exact strings the owner has decided to publish on purpose (docs/publish-allow.txt, one per line)."""
    f = root / ALLOW_FILE
    if not f.exists():
        return []
    lines = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def scan_text(
    text: str, needles: dict[str, str] | None = None, allow: Iterable[str] = ()
) -> list[tuple[int, str]]:
    """(line, kind) for everything in `text` that must not be published. Values are never returned.
    Strings in `allow` (decided on purpose) are ignored."""
    found: list[tuple[int, str]] = []
    needles = needles or {}
    for n, line in enumerate(text.splitlines(), 1):
        if FIXTURE_MARKER in line:
            continue
        for ok in allow:
            line = line.replace(ok, " ")
        kinds: set[str] = set()
        for kind, value in needles.items():
            if value and value.lower() in line.lower():
                kinds.add(kind)
        if any(not _fake_guid(g) for g in GUID.findall(line)):
            kinds.add("GUID")
        for m in EMAIL.finditer(line):
            if (
                m.group(1).lower() not in FAKE_DOMAINS and not m.group(1).isdigit()
            ):  # not an IP address
                kinds.add("EMAIL_OR_UPN")
        for m in HOST.finditer(line):
            if m.group(1).lower() not in FAKE_DOMAINS and m.group(1) != "<tenant>":
                kinds.add("TENANT_HOST")
        for kind, rx in PATTERNS.items():
            for m in rx.finditer(line):
                if kind == "DRIVE_ITEM_ID" and (
                    FAKE_ITEM_ID.match(m.group(0)) or m.group(0) in BASE36
                ):
                    continue
                kinds.add(kind)
        found += [(n, k) for k in sorted(kinds)]
    return found


def env_needles(env_file: Path) -> dict[str, str]:
    """Exact values from a local .env (identifiers and secrets). Empty if there is no file."""
    if not env_file.exists():
        return {}
    env: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    out: dict[str, str] = {}
    for key in (
        "LEAKLAB_TENANT_ID",
        "LEAKLAB_ALLOWED_TENANT_ID",
        "LEAKLAB_DRIVER_CLIENT_ID",
        "LEAKLAB_PERSONA_CLIENT_ID",
        "LEAKLAB_INGEST_APP_ID",
        "LEAKLAB_CERT_THUMBPRINT",
        "LEAKLAB_PERSONA_PASSWORD",
        "LEAKLAB_INGEST_APP_SECRET",
        "LEAKLAB_PG_PASSWORD",
        "LEAKLAB_API_OPERATOR_KEY",
        "LEAKLAB_SP_HOST",
        "LEAKLAB_ADMIN_UPN",
    ):
        if len(env.get(key, "")) >= 6:
            out[key.removeprefix("LEAKLAB_")] = env[key]
    host = env.get("LEAKLAB_SP_HOST", "")
    if "." in host:
        out["TENANT_NAME"] = host.split(".")[0].removesuffix("-my")
    endpoint = re.match(r"https?://([^.]+)\.", env.get("LEAKLAB_SEARCH_ENDPOINT", ""))
    if endpoint and len(endpoint.group(1)) >= 4:
        out["SEARCH_SERVICE_NAME"] = endpoint.group(1)
    return out


def tracked_files(root: Path) -> list[str]:
    """Files git tracks, plus untracked files that are not ignored (they would be committed next)."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [f for f in out.split("\n") if f]


def check(
    root: Path, files: Iterable[str] | None = None, needles: dict[str, str] | None = None
) -> tuple[dict[str, list[str]], list[Finding]]:
    """({'PRIVATE': [...], 'UNLISTED': [...]} of tracked files, findings in tracked PUBLIC files)."""
    names = list(files if files is not None else tracked_files(root))
    needles = env_needles(root / ".env") if needles is None else needles
    allow = allowed_values(root)
    wrong: dict[str, list[str]] = {"PRIVATE": [], "UNLISTED": []}
    findings: list[Finding] = []
    for name in names:
        kind = classify(name)
        if kind != "PUBLIC":
            wrong[kind].append(name)
            continue
        path = root / name
        if name in SCAN_EXEMPT or path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings += [Finding(name, n, k) for n, k in scan_text(text, needles, allow)]
    return wrong, findings


def allowlisted_files(
    root: Path, files: Iterable[str] | None = None
) -> tuple[list[str], list[str]]:
    """(PUBLIC files, UNLISTED files) among the tracked and not-ignored files. Private files are left out."""
    names = list(files if files is not None else tracked_files(root))
    public = [n for n in names if classify(n) == "PUBLIC" and (root / n).is_file()]
    unlisted = [n for n in names if classify(n) == "UNLISTED"]
    return public, unlisted
