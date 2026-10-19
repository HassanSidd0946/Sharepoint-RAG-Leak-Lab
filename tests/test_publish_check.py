"""The publication check: classification of paths and the scan of public text.

(This file is exempt from the scan itself: its fixtures are fake identifiers on purpose.)"""

from pathlib import Path

from leaklab.replay.publish_check import check, classify, scan_text

NL = chr(10)


def test_paths_are_public_private_or_unlisted() -> None:
    assert classify("src/leaklab/spec.py") == "PUBLIC"
    assert classify("docs/FINDINGS.md") == "PUBLIC"
    assert classify("optimus-main/public/replays/index.json") == "PUBLIC"
    assert classify(".env.example") == "PUBLIC"
    assert classify(".env") == "PRIVATE"
    assert classify("probes/out/access_probe.json") == "PRIVATE"
    assert classify("runs/benchmark_v2/report.json") == "PRIVATE"
    assert classify("PROMPT_25.md") == "PRIVATE"
    assert classify("beam-main/app/page.tsx") == "PRIVATE"
    assert classify("optimus-main/node_modules/x/index.js") == "PRIVATE"
    assert classify("somewhere/else.txt") == "UNLISTED"


def test_the_scan_finds_identifiers_and_secrets() -> None:
    text = NL.join(
        [
            "tenant 3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "mail alice@realcorp.com",
            "host realcorp.sharepoint.com",
            'client_secret = "abcdefghijklmnop1234"',
            "-----BEGIN PRIVATE KEY-----",
            "item 01ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        ]
    )
    assert set(scan_text(text)) == {
        (1, "GUID"),
        (2, "EMAIL_OR_UPN"),
        (3, "TENANT_HOST"),
        (4, "SECRET_VALUE"),
        (5, "PRIVATE_KEY"),
        (6, "DRIVE_ITEM_ID"),
    }


def test_the_scan_leaves_placeholders_alone() -> None:
    text = NL.join(
        [
            "00000000-0000-4000-8000-0000000000a1",
            "bob@contoso.com, contoso.sharepoint.com, <tenant>.sharepoint.com, svc.search.windows.net",
            "00000003-0000-0ff1-ce00-000000000000",
            "item 01FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE",
            "postgresql://leaklab:pw@127.0.0.1:1/leaklab",
            "_B36 = 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "alice@realcorp.com  # publish-check: fixture",
        ]
    )
    assert scan_text(text) == []


def test_exact_values_from_the_env_are_found_by_kind_only() -> None:
    assert scan_text("the tenant is acmecorp here", {"TENANT_NAME": "acmecorp"}) == [
        (1, "TENANT_NAME")
    ]


def test_check_reports_private_and_unlisted_tracked_files_and_findings(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "x = 1" + NL + "owner = 'bob@realcorp.com'" + NL, encoding="utf-8"
    )
    wrong, findings = check(tmp_path, ["src/a.py", "probes/out/x.json", "other.txt"], needles={})
    assert wrong == {"PRIVATE": ["probes/out/x.json"], "UNLISTED": ["other.txt"]}
    assert [(f.path, f.line, f.kind) for f in findings] == [("src/a.py", 2, "EMAIL_OR_UPN")]
