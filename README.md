

# LeakLab: does permission-aware RAG over SharePoint stop returning content when access changes?

LeakLab measures how long a revoked user keeps getting a document after a permission change in Microsoft 365, in three
systems read through the same polling loop: **SharePoint itself**, **Azure AI Search's SharePoint indexer** (basic ACL
configuration) and **a small revocation layer** built in this repository. It also tests whether an agent's shared
answer cache and memory leak content even when retrieval is correct.

A leak is exactly one thing: a unique canary string served to a user whom an independent model of the intended
permissions (the *oracle*) says must not see it at that moment. No language model judges anything.

## Headline result

After removing a user from an **Entra security group**, SharePoint kept serving the file for a median of
**45.7 min** [provisional (n = 1)] before the first
denial. Azure AI Search in its basic ACL configuration stopped after
**1.6 min** [provisional (n = 1)], the
revocation layer after **10.3 s**
[provisional (n = 1)].

This is specific to **Entra-group changes**; direct and SharePoint-group grants behave differently (see the write-up).
The agent tests are separate: the leaked content was delivered from the agent's shared cache and memory, not from
retrieval.



## Scope and limitations

Generated from the benchmark report (1 finished run(s), 5
replicate(s), 16974 reads). These are the report's own caveats:

- Sample sizes: each scenario x system cell has between 1 and 1 complete replicates (planned: S1-direct 1, S1-spgroup 1, S1-entra 7, S2-nested-group-removal 7, S4-move-into-restricted 1). Minimum, median and maximum are descriptive; with so few replicates they are not estimates of a distribution, and no confidence interval or significance test is given. Cells below 3 replicates: S1-direct/setup0 (n=1), S1-direct/setup_a_basic (n=1), S1-direct/setup_b (n=1), S1-spgroup/setup0 (n=1), S1-spgroup/setup_a_basic (n=1), S1-spgroup/setup_b (n=1), S1-entra/setup0 (n=1), S1-entra/setup_a_basic (n=1), S1-entra/setup_b (n=1), S2-nested-group-removal/setup0 (n=1), S2-nested-group-removal/setup_a_basic (n=1), S2-nested-group-removal/setup_b (n=1), S4-move-into-restricted/setup0 (n=1), S4-move-into-restricted/setup_a_basic (n=1), S4-move-into-restricted/setup_b (n=1).
- System labels: 'setup0' reads SharePoint itself (no search layer) and is the reference. 'setup_a_basic' is Microsoft's SharePoint indexer with ACL ingestion in its plain configuration: users and Entra groups only, no SharePoint-group support, the smallest indexer schedule the service accepts (PT5M); it is not a tuned or best-case Setup A. Setup B queries a separate, unfiltered index over the same SharePoint data source; Setup A uses the ACL-filtered index.
- Scope: only the 7 persona accounts (p-ceo, p-finana, p-finlead, p-hr, p-intern, p-nest, p-plateng) are measured. The tenant admin holds full control and appears in every ACL; access held by the admin is out of scope.
- Single tenant: every run used the same tenant, SharePoint site and search service. Results may not transfer to other tenants, sizes or configurations; the corpus is small (45 documents).
- Dates: runs between 2026-10-18 11:46:27Z and 2026-10-18 12:51:06Z (UTC); this report was generated 2026-10-18 15:30:28Z. Replicates ran one after another, not interleaved, so time-of-day and service-side drift are not controlled.
- Method: read interval 5 s (every time below has that resolution); 'converged' means 10 conforming reads in a row; polling stopped 60 minutes after the change, so a DID_NOT_CONVERGE replicate is censored at the cap and its numbers are lower bounds. Every replicate started from a freshly generated tenant.
- Baseline: the baseline length alternates by run (1 min in runs [1, 3, 5, 7]; 10 min in runs [2, 4, 6]) for S1-entra. The other scenarios in a run share its baseline. The 'by baseline' rows separate the numbers.
- Data quality: 1 replicate(s) started from an unstable baseline for that system; 1 replicate(s) had nothing to revoke (the pair was not served before the change) and are excluded from first-deny and leak statistics; 36 planned replicate(s) have no result yet (not run, or the run stopped early).
- Setup B start-up: the runner waited for /ready before the baseline in 1 run(s); time to ready 76.628-76.628 s (median 76.628 s). Lockouts are counted only from reads after that point. The first sync was already complete in 1 of 1 run(s).
- Index cleanup: before 1 run(s) the documents of earlier tenant generations were deleted from both indexes (90 stale found, 90 deleted in total).
- Setup B is measured through its HTTP API with the persona's own token. Its index is refreshed by its own indexer on its own 5-minute cycle, so content freshness is comparable to Setup A's but not simultaneous. Post-filtering over a k*5 window loses results (273 recall samples, see the table).

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

- `LEAKLAB_TENANT_ID`
- `LEAKLAB_ALLOWED_TENANT_ID`
- `LEAKLAB_DRIVER_CLIENT_ID`
- `LEAKLAB_CERT_PATH`
- `LEAKLAB_CERT_PUBLIC_PATH`
- `LEAKLAB_CERT_THUMBPRINT`
- `LEAKLAB_PERSONA_CLIENT_ID`
- `LEAKLAB_PERSONA_PASSWORD`
- `LEAKLAB_SITE_URL`
- `LEAKLAB_SP_HOST`
- `LEAKLAB_USAGE_LOCATION`
- `LEAKLAB_ADMIN_UPN`
- `LEAKLAB_SEARCH_ENDPOINT`
- `LEAKLAB_INGEST_APP_ID`
- `LEAKLAB_INGEST_APP_SECRET`
- `LEAKLAB_PG_PASSWORD`
- `LEAKLAB_PG_DSN`
- `LEAKLAB_API_AUDIENCE`
- `LEAKLAB_API_OPERATOR_KEY`

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
