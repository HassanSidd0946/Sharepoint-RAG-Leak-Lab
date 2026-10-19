# What becomes public

Only what is listed under **Public** is published. Everything else stays private. The list is enforced by
`uv run python -m leaklab.replay publish-check` (`src/leaklab/replay/publish_check.py`): it fails if git would publish a
private or unlisted file, and scans every public file for identifiers and secrets. It reads `.env` locally to look
for the exact values in it and prints the file, the line and the kind of finding, never the value.

## Public

| What | Where |
|---|---|
| Source code | `src/`, `tests/`, `scripts/`, `probes/*.py` (the probe scripts, not their output), `optimus-main/` (the replay viewer; not `node_modules` or `.next`) |
| Configuration of the lab | `tenant_spec.yaml`, `pyproject.toml`, `uv.lock`, `docker-compose.yml`, `.env.example` (names only), `.gitignore` |
| Docs | everything under `docs/`: `FINDINGS.md`, `ARCHITECTURE.md`, `OVERNIGHT.md`, `UI_REUSE_PLAN.md`, `schemas/`, `writeup/` (template and `meta.yaml`), `video/`, `README.template.md` |
| Project files | `README.md` (generated from `docs/README.template.md`), `LICENSE` (Apache-2.0), `CLAUDE.md` |
| The exported results | `optimus-main/public/replays/`: the replay bundles, `index.json`, `summary.json`, `report.json`, `playback.json`; `optimus-main/public/meta.json`. Each bundle and the summary and report copies pass the redaction scanner when they are written |
| The figures | `docs/figures/` (SVG and PNG, drawn from the exported bundles) |

## Private

| What | Why |
|---|---|
| `runs/` (journals, per-run reports, generation registry, HTML report, search service file) | raw measurement data with tenant object ids |
| `probes/out/` | raw responses of the probes: tenant id, app ids, admin e-mail, item and drive ids |
| `.env`, `.env.*` (except `.env.example`), `certs/`, `*.pem`, `*.pfx`, `*.key` | secrets |
| `PROMPT_*.md`, `.claude/` | working prompts and local tool configuration |
| `docs/*HANDOFF.md` | internal hand-off reports (they quote the first benchmark, which is not published) |
| `beam-main/`, `pulse-metrics-main/` | the third-party v0 template sources the UI was built from |
| `DAY0_DAY1_RUNBOOK.md` | the setup log of the test tenant |

## How the public repository is built

The private repository is never made public and its history is never rewritten. The public repository is built from the
allowlist above into a **new folder with one clean first commit**:

```bash
uv run python -m leaklab.replay build-public --out ../leaklab-public
```

It (1) refuses a non-empty folder and any tracked file that is neither public nor private, (2) copies only the public
files, (3) writes `tenant_spec.yaml` with the placeholders `<tenant>.onmicrosoft.com` and `<site-url>` (the real file stays
private; the README documents them), (4) runs `publish-check` on the new folder, (5) runs gitleaks on it, (6) runs
`git init`, one commit, and gitleaks again over that history. It stops at the first failing step. It pushes nothing.
It is run once, after the final export (planned 2026-10-24), not before.

**Spec hashes.** The `spec_sha256` and `plan_sha256` in the published `report.json` (and in any journal) are hashes of the
private, resolved `tenant_spec.yaml`. The published copy has placeholders, so its hash is different.

**Values published on purpose** are listed in `docs/publish-allow.txt` (today: the contact address in
`docs/writeup/meta.yaml`); `publish-check` ignores exactly those strings.

## State of the private repository (2026-10-19)

The private repository tracks some files that are not public (`probes/out/`, `beam-main/`, `pulse-metrics-main/`,
`PROMPT_*.md`, `.claude/`, `DAY0_DAY1_RUNBOOK.md`). That is fine: the private repository stays private, and the allowlist
build leaves them out. `publish-check` run on the private repository reports them as "private files that git tracks"; run
on the new folder it must report nothing.

`tenant_spec.yaml` holds the real tenant domain and site URL in the private repository. It is not changed while the
overnight benchmark runs (its hash identifies the recorded runs).
