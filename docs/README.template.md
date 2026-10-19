<!--
README TEMPLATE. Filled by:
  uv run python -m leaklab.replay writeup --template docs/README.template.md --out README.md \
      --video none --replays optimus-main/public/replays
Every number comes from summary.json; the fill fails on an unknown token. Never type a number here.
-->

# LeakLab: does permission-aware RAG over SharePoint stop returning content when access changes?

LeakLab measures how long a revoked user keeps getting a document after a permission change in Microsoft 365, in three
systems read through the same polling loop: **SharePoint itself**, **Azure AI Search's SharePoint indexer** (basic ACL
configuration) and **a small revocation layer** built in this repository. It also tests whether an agent's shared
answer cache and memory leak content even when retrieval is correct.

A leak is exactly one thing: a unique canary string served to a user whom an independent model of the intended
permissions (the *oracle*) says must not see it at that moment. No language model judges anything.

## Headline result

After removing a user from an **Entra security group**, SharePoint kept serving the file for a median of
**{{summary.S1-entra.setup0.first_deny_s.median|dur}}** [{{summary.S1-entra.setup0.n_label}}] before the first
denial. Azure AI Search in its basic ACL configuration stopped after
**{{summary.S1-entra.setup_a_basic.first_deny_s.median|dur}}** [{{summary.S1-entra.setup_a_basic.n_label}}], the
revocation layer after **{{summary.S1-entra.setup_b.first_deny_s.median|dur}}**
[{{summary.S1-entra.setup_b.n_label}}].

This is specific to **Entra-group changes**; direct and SharePoint-group grants behave differently (see the write-up).
The agent tests are separate: the leaked content was delivered from the agent's shared cache and memory, not from
retrieval.

{{site.links.writeup_line}}

## Scope and limitations

Generated from the benchmark report ({{summary.totals.runs}} finished run(s), {{summary.totals.replicates}}
replicate(s), {{summary.totals.reads}} reads). These are the report's own caveats:

{{summary.caveats}}

## What is in the repository

| Path | What |
|---|---|
| `src/leaklab/generator/` | builds the synthetic tenant from `tenant_spec.yaml`; `teardown` removes only prefixed objects |
| `src/leaklab/oracle/` | the independent expected-access model (a test enforces that it imports no engine code) |
| `src/leaklab/runner/`, `benchmark/` | the polling loop, the scenarios, journals and the benchmark report |
| `src/leaklab/setup_a/`, `setup_b/` | Azure AI Search provisioning; the revocation layer (FastAPI) |
| `src/leaklab/agent/` | the S6 agent (LangGraph) with a shared cache and memory, naive and guarded |
| `src/leaklab/replay/` | exports journals to redacted replay bundles; fills the write-up |
| `optimus-main/` | the static replay viewer (Next.js) |
| `docs/` | `FINDINGS.md` (every verified API behaviour), architecture, schemas, the write-up template |

## Prerequisites

- A **Microsoft 365 test tenant** you can destroy things in (not a production tenant): the lab creates and deletes
  users, groups and files, and licenses a few users.
- An **Azure subscription** with an Azure AI Search service (Basic tier or higher) for the indexer runs.
- Python 3.12 with [uv](https://docs.astral.sh/uv/), Docker (Setup B keeps its state in Postgres) and Node.js for the viewer.
- An Entra app with a certificate (the driver app), a second app for persona sign-ins, and an ingestion app for the
  SharePoint indexer. `docs/FINDINGS.md` records the permissions each needs.

Configuration is read from environment variables or a `.env` file (see `.env.example`; never commit `.env`). The
variable names:

{{env.names}}

## `tenant_spec.yaml` placeholders

`tenant_spec.yaml` describes the synthetic tenant (users, groups, folders, canaries, scenarios). Two values in it are
specific to a real tenant and are placeholders in the published copy:

- `naming.domain: "<tenant>.onmicrosoft.com"`: replace `<tenant>` with your test tenant's domain;
- `site.url: "<site-url>"`: replace with your SharePoint site URL (`https://<tenant>.sharepoint.com/sites/<site>`); its host
  must equal `LEAKLAB_SP_HOST`.

The published result files (`optimus-main/public/replays/`) were produced with the real, resolved spec. The spec hashes
recorded in them (`spec_sha256`, `plan_sha256` in `report.json`) therefore refer to that private file, not to the
placeholder copy: you cannot re-export the published journals against it, and your own runs will have their own hashes.

## The tenant guard

Every script that writes to a tenant calls `config.assert_test_tenant()` first. It compares the tenant id inside the
acquired token with `LEAKLAB_ALLOWED_TENANT_ID` and aborts on a mismatch, on an empty value or when the variable is not
set. Generated objects carry the prefix from `tenant_spec.yaml`, and teardown deletes only objects with that prefix.
Do not point this at a tenant you care about.

## Run it

```bash
uv sync
docker compose up -d                                         # Postgres for Setup B
uv run python -m leaklab.generator generate                  # build the test tenant
uv run python -m leaklab.benchmark run --dir runs/benchmark  # run the scenarios on all three systems
uv run python -m leaklab.benchmark report --dir runs/benchmark
uv run python -m leaklab.replay export --runs runs/benchmark --out optimus-main/public/replays
cd optimus-main && npm install && npm run dev                # the replay viewer
```

Checks: `uv run ruff check .`, `uv run mypy src`, `uv run pytest` (tests marked `live` need a tenant and are skipped by
default).

## License

Apache-2.0, see `LICENSE`.
