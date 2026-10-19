"""Build the public repository from the allowlist into a NEW folder with ONE clean first commit.

  uv run python -m leaklab.replay build-public --out ../leaklab-public

What it does, in order (it stops at the first failing step and leaves the folder for inspection):
  1. refuses a non-empty output folder, and refuses to run if a tracked file is neither public nor private
     (docs/PUBLISH.md must list every kind of file);
  2. copies ONLY the allowlisted (PUBLIC) files; private files are never read;
  3. writes tenant_spec.yaml with placeholders for the two tenant-specific values (the domain and the site
     URL); the real file stays in the private repository;
  4. runs publish-check on the NEW folder (private/unlisted files, identifiers, secrets, the exact values from
     the local .env);
  5. runs gitleaks on the new folder (Docker image zricethezav/gitleaks);
  6. git init, one commit; gitleaks again over that history.

The private history is never rewritten and never copied.
"""

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from leaklab.replay.publish_check import allowlisted_files, check, env_needles

DOMAIN_PLACEHOLDER = "<tenant>.onmicrosoft.com"
SITE_URL_PLACEHOLDER = "<site-url>"
SPEC_FILE = "tenant_spec.yaml"
GITLEAKS_IMAGE = "zricethezav/gitleaks:latest"
COMMIT_MESSAGE = (
    "Initial public release\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
)

DOMAIN_LINE = re.compile(r'^(\s*domain:\s*)"[^"]*"', re.MULTILINE)
URL_LINE = re.compile(r'^(\s*url:\s*)"https?://[^"]*"', re.MULTILINE)


class BuildError(Exception):
    """A step of the public build failed; the message says which."""


def placeholder_spec(text: str) -> str:
    """tenant_spec.yaml with the tenant domain and the site URL replaced by placeholders."""
    text, n_domain = DOMAIN_LINE.subn(rf'\1"{DOMAIN_PLACEHOLDER}"', text, count=1)
    text, n_url = URL_LINE.subn(rf'\1"{SITE_URL_PLACEHOLDER}"', text, count=1)
    if n_domain != 1 or n_url != 1:
        raise BuildError(
            "tenant_spec.yaml: could not find exactly one `domain:` and one `url:` line to replace"
        )
    return text


GitleaksRunner = Callable[[Path, bool], tuple[int, str]]


def run_gitleaks(folder: Path, with_history: bool) -> tuple[int, str]:
    """gitleaks in Docker over `folder` (exit code, output). Secrets are redacted in its output."""
    args = [
        "docker", "run", "--rm", "-v", f"{folder.resolve()}:/repo:ro", GITLEAKS_IMAGE,
        "detect", "--source", "/repo", "--redact", "--no-banner",
    ]  # fmt: skip
    if not with_history:
        args.append("--no-git")
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr)[-2000:]


@dataclass
class BuildReport:
    out: Path
    files: list[str] = field(default_factory=list)
    commit: str = ""
    steps: list[str] = field(default_factory=list)


def _git(out: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=out, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def build_public(
    root: Path,
    out: Path,
    *,
    files: list[str] | None = None,
    gitleaks: GitleaksRunner | None = run_gitleaks,
    needles: dict[str, str] | None = None,
) -> BuildReport:
    """Build the public repository in `out` (see the module docstring). `gitleaks=None` skips that step."""
    report = BuildReport(out=out)
    if out.exists() and any(out.iterdir()):
        raise BuildError(f"{out} is not empty")
    public, unlisted = allowlisted_files(root, files)
    if unlisted:
        raise BuildError(
            f"files that are neither public nor private (list them in PUBLISH.md): {unlisted[:10]}"
        )
    if SPEC_FILE not in public:
        raise BuildError(f"{SPEC_FILE} is not in the allowlist")
    report.steps.append(f"allowlist: {len(public)} files")

    out.mkdir(parents=True, exist_ok=True)
    for name in public:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == SPEC_FILE:
            target.write_text(
                placeholder_spec((root / name).read_text(encoding="utf-8")), encoding="utf-8"
            )
        else:
            shutil.copyfile(root / name, target)
        report.files.append(name)
    report.steps.append("copied; tenant_spec.yaml written with placeholders")

    present = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    wrong, findings = check(
        out, present, needles=env_needles(root / ".env") if needles is None else needles
    )
    if any(wrong.values()) or findings:
        shown = [f"{f.path}:{f.line} {f.kind}" for f in findings[:10]]
        raise BuildError(f"publish-check failed on the new folder: {wrong} {shown}")
    report.steps.append("publish-check: clean")

    if gitleaks is not None:
        code, text = gitleaks(out, False)
        if code != 0:
            raise BuildError(f"gitleaks (files) found something:\n{text}")
        report.steps.append("gitleaks (files): clean")

    _git(out, "init", "-q", "-b", "main")
    _git(out, "add", "-A")
    _git(out, "commit", "-q", "-m", COMMIT_MESSAGE)
    report.commit = _git(out, "rev-parse", "--short", "HEAD")
    if _git(out, "rev-list", "--count", "HEAD") != "1":
        raise BuildError("the new repository must have exactly one commit")
    report.steps.append(f"git: one commit {report.commit}")

    if gitleaks is not None:
        code, text = gitleaks(out, True)
        if code != 0:
            raise BuildError(f"gitleaks (history) found something:\n{text}")
        report.steps.append("gitleaks (history): clean")
    return report
