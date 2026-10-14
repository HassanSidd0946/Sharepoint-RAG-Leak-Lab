# CLAUDE.md — SharePoint RAG Leak Lab

## What this project is

A benchmark and prototype that measures whether permission-aware RAG over SharePoint
actually stops returning content when access changes, and a custom layer that closes the gaps.

- **Setup A**: Microsoft-native. Azure AI Search SharePoint indexer with ACL ingestion (preview),
  configured exactly as Microsoft's documentation says. We do NOT tune Setup A to make it fail.
- **Setup B**: our FastAPI layer in front of the same Azure index: revocation overlay,
  permission-aware cache/memory, delta-based change detection.
- **Test harness**: synthetic tenant generator + independent oracle + scenario runner.
  Leaks are detected by exact canary-string match, never by LLM judgment.

The output must be honest. Every claim in reports and the demo must come from a real run.

## Non-negotiable rules

### 1. Probe before you build
- NEVER assume how Microsoft Graph, SharePoint REST, Entra ID or Azure AI Search behaves.
- Before writing code that depends on an API's behavior, write or extend a probe in `probes/`,
  run it (or ask the user to run it), and save the raw response to `probes/out/<name>.json`.
- Record every verified behavior in `docs/FINDINGS.md` with: date, endpoint, what we expected,
  what actually happened, link to the doc page if any.
- If a behavior is not yet verified, mark the code with `# UNVERIFIED: <what needs checking>`
  and list it in `docs/FINDINGS.md` under "Open questions". Do not hide uncertainty.
- Never invent endpoints, request fields, permission names or response shapes. If you are not
  sure an endpoint exists, say so and propose a probe.
- Prefer Graph v1.0. If something only exists in `/beta` or a preview api-version, flag it
  explicitly in the code and in FINDINGS.md.

### 2. The oracle is independent
- `src/leaklab/oracle/` computes "who should see which canary" ONLY from `tenant_spec.yaml`
  plus the list of applied mutations.
- The oracle must NEVER import from `setup_a`, `setup_b`, `graph_client`, `sp_rest` or any engine
  code. A test enforces this (`tests/test_oracle_isolation.py`).
- When SharePoint's real behavior differs from what we assumed, update the oracle model
  deliberately and document why in FINDINGS.md. Never patch the oracle to make a test pass.

### 3. Fail closed
- Any permission check that cannot be completed (timeout, missing data, stale data, unknown
  principal) must DENY, never allow.
- Unknown principal types or claims (e.g. special SharePoint claims) → deny + log a warning.
- Caches and memory return content only after re-checking the current user's access to every
  source document.

### 4. Test tenant only — safety guard
- Every script that writes to a tenant must call `config.assert_test_tenant()` first, which
  checks the token's tenant ID against `LEAKLAB_ALLOWED_TENANT_ID` and aborts otherwise.
- Generated objects use the prefix from `tenant_spec.yaml` (`naming.prefix`) so teardown can
  find them. Teardown only deletes objects with that prefix.

### 5. Secrets
- Secrets come only from environment variables / `.env` (loaded via pydantic-settings).
- Never write passwords, tokens, private keys or client secrets into code, YAML, logs,
  probe outputs or FINDINGS.md. Redact tokens in saved probe output (keep only decoded,
  non-secret claims like `tid`, `aud`, `scp`, `roles`, `oid`).
- `.env`, `certs/`, `*.pem`, `*.pfx`, `probes/out/*.raw.json` are git-ignored.

## Engineering standards

- Python 3.12, `src/` layout, `uv` for env management.
- Full type hints; `mypy --strict` must pass for `src/`. `ruff` for lint + format.
- HTTP via `httpx` (sync is fine for now). Handle 429/503 with `Retry-After` and exponential
  backoff. Handle `@odata.nextLink` paging.
- Auth via `msal`: certificate-based app-only for the driver app; persona tokens via
  username/password (test tenant only) with device-code fallback.
- Pydantic v2 models for the spec and for every structured result.
- CLI entry points with `typer`; human output with `rich`; machine output as JSON.
- Small modules with a docstring at the top explaining purpose and inputs/outputs.
  The owner must be able to maintain this code without an AI assistant.
- Every non-trivial function gets a pytest test. Network-dependent tests are marked
  `@pytest.mark.live` and skipped by default.

## Workflow for each task

1. Restate the task in 2–3 lines and list the files you will touch.
2. If the task depends on unverified API behavior → write/extend a probe first and stop.
3. Implement in small steps; run `ruff`, `mypy`, `pytest` before saying you're done.
4. Summarize: what changed, what is verified, what is still UNVERIFIED.
5. Suggest a git commit message. Do not commit secrets.

## Repository layout

```
leaklab/
  CLAUDE.md
  tenant_spec.yaml
  pyproject.toml
  .env.example
  docs/FINDINGS.md          # verified API behavior + open questions
  probes/                   # small scripts that check real API behavior
    out/                    # saved (redacted) probe responses
  src/leaklab/
    config.py               # settings + assert_test_tenant()
    auth.py                 # MSAL: app-only cert + persona tokens
    graph_client.py         # thin Graph wrapper (retry, paging)
    sp_rest.py              # thin SharePoint REST wrapper (cert auth)
    spec.py                 # pydantic models for tenant_spec.yaml
    generator/              # builds the tenant from the spec (later)
    oracle/                 # independent expected-access model (later)
    setup_a/                # Azure AI Search provisioning + query (later)
    setup_b/                # FastAPI overlay layer (later)
    agent/                  # LangGraph agent with cache/memory (later)
    runner/                 # scenario runner (later)
    report/                 # HTML/JSON reports (later)
  tests/
```

## Known areas of uncertainty (verify with probes, do not assume)

- How to break permission inheritance on a folder/file: Graph may not support it directly;
  SharePoint REST `breakroleinheritance` likely needed (requires certificate app-only auth).
- How Entra security groups appear as SharePoint principals (claims format) and how to grant
  them access via REST role assignments.
- Whether Graph `driveItem` permissions show inherited vs. unique grants, and how.
- How SharePoint groups (site Owners/Members/Visitors) and special claims such as
  "Everyone except external users" appear in permission responses.
- What happens to a file's unique permissions when it is moved to another folder.
- Whether unlicensed users can be granted access and be trimmed correctly in Azure AI Search.
- Which token audience/scope the Azure AI Search ACL query requires for the end user.
