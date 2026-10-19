"""build-public on a fake repository in a temp folder with a fake gitleaks runner (no Docker, no network).

Checks: allowlist copy, placeholder spec, checks on the new folder, one clean commit."""

import subprocess
from pathlib import Path

import pytest

from leaklab.replay.build_public import (
    DOMAIN_PLACEHOLDER,
    SITE_URL_PLACEHOLDER,
    BuildError,
    build_public,
    placeholder_spec,
)
from leaklab.replay.export import replicate_ordinals
from leaklab.replay.publish_check import allowed_values, scan_text

SPEC = (
    'naming:\n  prefix: "ll-"\n  domain: "acmecorp.onmicrosoft.com"   # verified\n'
    'site:\n  url: "https://acmecorp.sharepoint.com/sites/Lab"   # verified\n'
)
NEEDLES = {"TENANT_NAME": "acmecorp"}
FILES = [
    "src/a.py",
    "tests/test_a.py",
    "docs/note.md",
    "README.md",
    "tenant_spec.yaml",
    "probes/out/raw.json",
    "PROMPT_1.md",
    ".claude/launch.json",
    "beam-main/x.tsx",
]


def fake_repo(tmp_path: Path, **overrides: str) -> Path:
    root = tmp_path / "private"
    content = {
        "src/a.py": "x = 1\n",
        "tests/test_a.py": "def test(): pass\n",
        "docs/note.md": "notes\n",
        "README.md": "hello\n",
        "tenant_spec.yaml": SPEC,
        "probes/out/raw.json": '{"tenant": "acmecorp"}\n',
        "PROMPT_1.md": "private prompt\n",
        ".claude/launch.json": "{}\n",
        "beam-main/x.tsx": "x\n",
        **overrides,
    }
    for name, text in content.items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def clean_gitleaks(folder: Path, history: bool) -> tuple[int, str]:
    return 0, "ok"


def test_the_placeholder_spec_replaces_exactly_the_domain_and_the_site_url() -> None:
    out = placeholder_spec(SPEC)
    assert DOMAIN_PLACEHOLDER in out and SITE_URL_PLACEHOLDER in out
    assert "acmecorp" not in out and "verified" in out  # comments stay
    assert out.count("\n") == SPEC.count("\n")


def test_the_placeholder_spec_refuses_a_spec_it_does_not_understand() -> None:
    with pytest.raises(BuildError):
        placeholder_spec('naming:\n  prefix: "ll-"\n')


def test_the_build_copies_only_public_files_and_makes_one_commit(tmp_path: Path) -> None:
    root = fake_repo(tmp_path)
    out = tmp_path / "public"
    report = build_public(root, out, files=FILES, gitleaks=clean_gitleaks, needles=NEEDLES)
    assert sorted(report.files) == [
        "README.md",
        "docs/note.md",
        "src/a.py",
        "tenant_spec.yaml",
        "tests/test_a.py",
    ]
    for private in ("probes/out/raw.json", "PROMPT_1.md", ".claude/launch.json", "beam-main/x.tsx"):
        assert not (out / private).exists()
    spec = (out / "tenant_spec.yaml").read_text(encoding="utf-8")
    assert DOMAIN_PLACEHOLDER in spec and SITE_URL_PLACEHOLDER in spec and "acmecorp" not in spec
    assert (root / "tenant_spec.yaml").read_text(
        encoding="utf-8"
    ) == SPEC  # the private file is untouched
    count = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=out, capture_output=True, text=True, check=True
    )
    assert count.stdout.strip() == "1" and report.commit
    assert report.steps[-1] == "gitleaks (history): clean"


def test_an_identifier_in_a_public_file_stops_the_build_before_any_commit(tmp_path: Path) -> None:
    root = fake_repo(tmp_path, **{"docs/note.md": "see acmecorp.sharepoint.com\n"})
    out = tmp_path / "public"
    with pytest.raises(BuildError, match="publish-check failed"):
        build_public(root, out, files=FILES, gitleaks=clean_gitleaks, needles=NEEDLES)
    assert not (out / ".git").exists()


def test_a_gitleaks_finding_stops_the_build(tmp_path: Path) -> None:
    root = fake_repo(tmp_path)
    with pytest.raises(BuildError, match="gitleaks"):
        build_public(
            root,
            tmp_path / "public",
            files=FILES,
            gitleaks=lambda f, h: (1, "leak"),
            needles=NEEDLES,
        )


def test_a_file_that_is_neither_public_nor_private_stops_the_build(tmp_path: Path) -> None:
    root = fake_repo(tmp_path, **{"mystery.txt": "?\n"})
    with pytest.raises(BuildError, match="neither public nor private"):
        build_public(
            root,
            tmp_path / "public",
            files=[*FILES, "mystery.txt"],
            gitleaks=clean_gitleaks,
            needles=NEEDLES,
        )


def test_a_non_empty_output_folder_is_refused(tmp_path: Path) -> None:
    root = fake_repo(tmp_path)
    out = tmp_path / "public"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(BuildError, match="not empty"):
        build_public(root, out, files=FILES, gitleaks=clean_gitleaks, needles=NEEDLES)


def test_values_the_owner_decided_to_publish_are_not_reported(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "publish-allow.txt").write_text(
        "# why\nme@realcorp.com\n", encoding="utf-8"
    )
    allow = allowed_values(tmp_path)
    assert allow == ["me@realcorp.com"]
    assert scan_text("contact: me@realcorp.com", allow=allow) == []
    assert scan_text("contact: you@realcorp.com", allow=allow) == [(1, "EMAIL_OR_UPN")]


def test_replicate_numbers_follow_the_plan_not_the_run_number(tmp_path: Path) -> None:
    import json

    plan = {
        "runs": [
            {"index": 1, "scenarios": ["S1-direct", "S1-entra"]},
            {"index": 2, "scenarios": ["S1-entra"]},
            {"index": 3, "scenarios": ["S1-direct", "S1-spgroup"]},
            {"index": 4, "scenarios": ["S1-direct"]},
        ]
    }
    manifest = {"runs": [{"index": i, "journal": f"run_{i}.jsonl"} for i in (1, 2, 3, 4)]}
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    ords = replicate_ordinals(tmp_path, manifest)
    assert ords["run_1.jsonl"] == {"S1-direct": 1, "S1-entra": 1}
    assert ords["run_2.jsonl"] == {"S1-entra": 2}
    assert ords["run_3.jsonl"] == {"S1-direct": 2, "S1-spgroup": 1}
    assert ords["run_4.jsonl"] == {"S1-direct": 3}
    assert replicate_ordinals(tmp_path / "nowhere", manifest) == {}
