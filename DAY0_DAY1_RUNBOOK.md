# Day 0 + Day 1 Runbook — SharePoint RAG Leak Lab

Order of work today:
1. Do the **manual checklist** (Part A) first — the probes need real credentials.
2. Put `CLAUDE.md` and `tenant_spec.yaml` in the repo root.
3. Run the **Claude Code prompts** (Part B) one at a time. Use `/clear` between prompts.
   After each prompt: read the diff, run the tests yourself, commit.

Portal menu names change often. If a label below doesn't match exactly, search the portal for
the setting name — the setting itself is what matters.

---

## Part A — Manual checklist (portals)

### A1. Microsoft 365 test tenant
- [ ] Sign up for a **Microsoft 365 Business Basic** trial with a **new** tenant
      (e.g. `<tenant>.onmicrosoft.com`). Use a Payoneer/virtual USD card if your local card fails.
- [ ] Optional: apply to the Microsoft 365 Developer Program too; if you happen to qualify for
      the E5 sandbox, use that tenant instead.
- [ ] Admin center → Users → confirm your admin account has a license.
- [ ] Note the tenant **domain** → put it in `tenant_spec.yaml` (`naming.domain`, `site.url`).

### A2. Disable security defaults (TEST TENANT ONLY)
- [ ] Entra admin center → Identity → Overview → Properties → **Manage security defaults** → Disabled.
      Reason: synthetic persona users must get tokens without MFA registration prompts.
      Never do this in a real tenant.

### A3. SharePoint site
- [ ] SharePoint → Create site → **Communication site** named `LeakLab`
      (communication site keeps SharePoint groups explicit — needed for edge case 1).
- [ ] Note the URL (`https://<tenant>.sharepoint.com/sites/LeakLab`) → `tenant_spec.yaml`.

### A4. Certificate for app-only auth (local machine)
```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout certs/leaklab-driver.key -out certs/leaklab-driver.crt \
  -subj "/CN=leaklab-driver"
openssl x509 -in certs/leaklab-driver.crt -noout -fingerprint -sha1
```
- [ ] Copy the SHA1 fingerprint (remove colons) → `LEAKLAB_CERT_THUMBPRINT` in `.env`.
- [ ] Make sure `certs/` is in `.gitignore`.

### A5. App registration 1 — `leaklab-driver` (app-only, builds/inspects the tenant)
- [ ] Entra → App registrations → New registration → single tenant → no redirect URI.
- [ ] Certificates & secrets → **Certificates** → upload `certs/leaklab-driver.crt`.
- [ ] API permissions → Add → **Microsoft Graph → Application permissions**:
      `User.ReadWrite.All`, `Group.ReadWrite.All`, `Sites.FullControl.All`, `Files.ReadWrite.All`
- [ ] API permissions → Add → **SharePoint → Application permissions**: `Sites.FullControl.All`
      (needed for SharePoint REST calls such as breaking inheritance).
- [ ] **Grant admin consent** for the tenant. All rows must show a green check.
- [ ] Note **Application (client) ID** and **Directory (tenant) ID**.

### A6. App registration 2 — `leaklab-persona` (delegated, acts as persona users)
- [ ] New registration → single tenant.
- [ ] Authentication → Advanced settings → **Allow public client flows: Yes**.
- [ ] API permissions → **Microsoft Graph → Delegated**:
      `User.Read`, `Files.Read.All`, `Sites.Read.All`, `GroupMember.Read.All`
- [ ] Grant admin consent.
- [ ] Note the client ID.
      (Azure AI Search delegated permission gets added on Day 4, per Microsoft's ACL docs.)

### A7. Azure (do NOT create the Search service yet)
- [ ] Create/confirm an Azure subscription (use free trial credit if your account is eligible).
- [ ] Create resource group `rg-leaklab`.
- [ ] Cost Management → **Budgets** → create a $50 budget with email alerts.
- [ ] Check Microsoft's docs for which regions support the SharePoint ACL indexer preview.
      Create the Search service (Basic tier) only on **Day 4** — it bills hourly.

### A8. Repo + .env
- [ ] Private GitHub repo `leaklab`. Add `CLAUDE.md`, `tenant_spec.yaml`.
- [ ] Create `.env` (Prompt 2 generates `.env.example` — copy it and fill in):
```
LEAKLAB_TENANT_ID=
LEAKLAB_ALLOWED_TENANT_ID=          # same as tenant ID; safety guard
LEAKLAB_DRIVER_CLIENT_ID=
LEAKLAB_CERT_PATH=certs/leaklab-driver.key
LEAKLAB_CERT_PUBLIC_PATH=certs/leaklab-driver.crt
LEAKLAB_CERT_THUMBPRINT=
LEAKLAB_PERSONA_CLIENT_ID=
LEAKLAB_PERSONA_PASSWORD=           # one strong password used for all synthetic users
LEAKLAB_SITE_URL=https://<tenant>.sharepoint.com/sites/LeakLab
LEAKLAB_SP_HOST=<tenant>.sharepoint.com
```

---

## Part B — Claude Code prompts (copy-paste, in order)

Start Claude Code in the repo root. Before Prompt 1, make sure `CLAUDE.md` and
`tenant_spec.yaml` are there.

### Prompt 1 — Scaffold
```
Read CLAUDE.md and tenant_spec.yaml fully before doing anything.

Task: scaffold the repository exactly as described in the "Repository layout" section of CLAUDE.md.
- Create pyproject.toml for Python 3.12 using uv, src layout, package name "leaklab".
  Runtime deps: msal, httpx, pydantic>=2, pydantic-settings, pyyaml, typer, rich, cryptography.
  Dev deps: pytest, ruff, mypy, types-PyYAML.
  Configure ruff (line length 100) and mypy (strict for src/), pytest (register a "live" marker,
  skip live tests unless --live is passed via a conftest.py option).
- Create empty packages with a module docstring for: config, auth, graph_client, sp_rest, spec,
  generator, oracle, setup_a, setup_b, agent, runner, report.
- Create probes/ and probes/out/ (with .gitkeep), docs/FINDINGS.md with sections
  "Verified behavior" and "Open questions" (copy the uncertainty list from CLAUDE.md into
  Open questions).
- Create .gitignore covering .env, certs/, *.pem, *.key, *.crt, *.pfx, probes/out/*.raw.json,
  .venv, __pycache__, .mypy_cache, .ruff_cache.
- Do NOT implement any logic yet.

Finish by running `uv sync`, `ruff check .`, `mypy src`, `pytest` and show me the tree.
```

### Prompt 2 — Config and safety guard
```
Implement src/leaklab/config.py:
- A pydantic-settings Settings class reading the LEAKLAB_* variables listed in .env.example
  (create .env.example with these keys and empty values): TENANT_ID, ALLOWED_TENANT_ID,
  DRIVER_CLIENT_ID, CERT_PATH, CERT_PUBLIC_PATH, CERT_THUMBPRINT, PERSONA_CLIENT_ID,
  PERSONA_PASSWORD (SecretStr), SITE_URL, SP_HOST.
- get_settings() cached accessor.
- assert_test_tenant(tid: str) -> None: raises a clear RuntimeError unless tid equals
  ALLOWED_TENANT_ID. This is the safety guard every tenant-writing script must call.
- A redact helper that, given a JWT, returns only non-secret decoded claims
  (tid, aud, scp, roles, oid, upn, appid, iss, exp) without verifying the signature.
Add unit tests for assert_test_tenant and the redact helper (use a fake unsigned JWT).
Run ruff, mypy, pytest.
```

### Prompt 3 — Spec models and validation
```
Implement src/leaklab/spec.py: pydantic v2 models that load and validate tenant_spec.yaml
exactly as it is structured now (naming, site, users, groups, folders, special_files,
documents, policy, scenarios). Add load_spec(path) -> TenantSpec.

Validation rules (raise clear errors):
- user ids and group ids are unique and do not collide with each other
- every member/member_group/principal ref points to an existing user or group
- group nesting has no cycles
- every folder's parent path exists (e.g. "Engineering/Platform-Secrets" needs "Engineering")
- folders with inherits=false must have at least one grant; inherits=true must have none
- every folder in folders has an entry in documents.folder_codes
- special_files reference existing folders

Also add a deterministic canary generator function generate_canaries(spec) ->
dict[str, list[str]] (folder path -> list of canaries) using naming.seed, so the same spec
always produces the same canaries, and all canaries are unique.

Tests: valid spec loads; each validation rule has a failing-case test; canaries are
deterministic across two runs and unique. Run ruff, mypy, pytest.
```

### Prompt 4 — Auth module
```
Implement src/leaklab/auth.py using msal:
- get_app_token(scope: str) -> str: certificate-based client credentials for the driver app
  (private key from CERT_PATH, thumbprint from CERT_THUMBPRINT). Scopes we will use:
  "https://graph.microsoft.com/.default" and "https://{SP_HOST}/.default".
- get_persona_token(upn: str, scopes: list[str]) -> str: PublicClientApplication with the
  persona client ID. Try username/password first (test tenant only; add a comment explaining
  why and that it must never be used in a real tenant); if it fails with an interaction-required
  error, fall back to device code flow and print the instructions.
- Use an in-memory token cache per process; do not persist tokens to disk.
- Never log tokens. Use config's redact helper whenever printing token info.
Unit-test what can be tested without network (e.g. scope construction, error mapping) with
mocks. Mark nothing as verified yet. Run ruff, mypy, pytest.
```

### Prompt 5 — token_probe.py
```
Write probes/token_probe.py (typer CLI):
- Acquire an app-only Graph token and an app-only SharePoint token for the driver app.
- Optionally (--persona <upn>) acquire a delegated Graph token for one persona user.
- For each token, print the redacted claims with rich and save them to
  probes/out/token_probe.json (redacted only).
- Call assert_test_tenant() with the tid from the app-only token before anything else.
- Then call GET https://graph.microsoft.com/v1.0/organization with the app token and print
  the tenant display name, as a basic connectivity check.
Exit non-zero with a clear message on any failure. Do not proceed to other probes.
After writing it, tell me exactly which command to run. I will run it and paste the output.
```

**→ Run it yourself.** Paste the output back to Claude Code with:
```
Here is the output of token_probe.py: <paste>. Check that the app token has the expected
application roles and the SharePoint token has the expected audience. Record verified facts
in docs/FINDINGS.md under "Verified behavior" with today's date. Do not change code unless
something failed.
```

### Prompt 6 — Graph client + graph_probe.py
```
Implement src/leaklab/graph_client.py: a thin GraphClient around httpx with
- base URL https://graph.microsoft.com/v1.0, bearer token from auth.get_app_token
- retries for 429/503/504 honoring Retry-After, exponential backoff with jitter, max 5 attempts
- get_paged(url) that follows @odata.nextLink
- methods only for what the probe needs: create_user, get_user_by_upn, create_security_group,
  add_user_to_group, add_group_to_group, list_transitive_member_of(user_id),
  delete_user, delete_group
Unit-test retry and paging with httpx MockTransport.

Then write probes/graph_probe.py (typer CLI), which calls assert_test_tenant() first and:
1. Creates (idempotently — reuse if already exists) two users "<prefix>probe-a" and
   "<prefix>probe-b" and three security groups "<prefix>probe-outer", "<prefix>probe-mid",
   "<prefix>probe-inner", using naming.prefix and naming.domain from tenant_spec.yaml and the
   persona password from settings.
2. Nests inner -> mid -> outer and puts probe-a in inner.
3. Calls transitiveMemberOf for probe-a and checks whether all three groups appear.
4. Measures and prints how long it takes until the nested membership is visible
   (poll every 5s, max 2 minutes).
5. Saves redacted responses to probes/out/graph_probe.json.
6. With --cleanup, deletes only the probe objects (prefix match) and nothing else.
Tell me the commands to run. Do not assume the results; we will record them in FINDINGS.md.
```

**→ Run it, paste the output, ask Claude Code to record findings.**

### Prompt 7 — SharePoint REST client + sp_probe.py
```
Implement src/leaklab/sp_rest.py: a thin SharePointRest client (httpx, app-only SharePoint
token from auth.get_app_token for scope https://{SP_HOST}/.default, Accept:
application/json;odata=nometadata, same retry policy as GraphClient).
Only add methods the probe needs. Mark every call whose behavior we have not verified with
# UNVERIFIED.

Then write probes/sp_probe.py (typer CLI), assert_test_tenant() first. Goal: discover, with
real responses, how to do the permission operations the generator will need. Steps:
1. Resolve the LeakLab site and its default document library via Graph
   (GET /sites/{SP_HOST}:/sites/LeakLab and its drive). Save the IDs.
2. Create folder "<prefix>probe-folder" and upload "<prefix>probe.txt" containing a test canary
   via Graph.
3. Read the file's permissions via Graph (GET .../items/{id}/permissions) BEFORE any change.
   Save raw (redacted) JSON — we want to see how inherited access is represented.
4. Break permission inheritance on the folder via SharePoint REST
   (ListItemAllFields/breakroleinheritance with copyRoleAssignments=false, clearSubscopes=true).
5. Resolve the security group "<prefix>probe-inner" as a SharePoint principal via
   web/ensureuser using its Entra claim, save the response, then add a read role assignment for
   it on the folder (look up the "Read" role definition ID via REST first — do not hardcode).
6. Read permissions again via BOTH Graph and SharePoint REST (roleassignments with
   Member and RoleDefinitionBindings expanded). Save both.
7. Also read the site's SharePoint groups (Owners/Members/Visitors) and their members via REST,
   and save how they appear — this informs edge case 1.
8. Print a rich summary table: operation, endpoint, HTTP status, short result.
9. --cleanup deletes only the probe folder.
If any step fails, stop, print the full (redacted) error body and suggest the next probe —
do not guess a workaround silently.
Tell me the commands to run.
```

**→ Run it, paste the output.** Then:

### Prompt 8 — Record findings and adjust the plan
```
Here are the outputs of graph_probe.py and sp_probe.py: <paste>.
1. Update docs/FINDINGS.md: move every confirmed behavior to "Verified behavior" with date,
   endpoint and a one-line result; keep anything still unclear in "Open questions".
2. Based ONLY on verified behavior, list what the generator (Week 1, Day 2) can rely on, and
   what still needs a probe.
3. Propose any changes to tenant_spec.yaml or spec.py that the findings require. Do not
   change the oracle semantics without explaining why.
4. Remove the # UNVERIFIED markers only from calls that are now verified.
Run ruff, mypy, pytest and give me a commit message.
```

---

## End-of-day check (Day 1 is done when)
- [ ] All three probes ran successfully against the real tenant.
- [ ] `docs/FINDINGS.md` lists how inheritance-breaking, group principals and permission
      reads actually work — with saved, redacted responses.
- [ ] `pytest`, `ruff`, `mypy` pass. Everything committed. No secrets in git.
- [ ] You know whether the generator can be built with Graph alone or needs SharePoint REST.
