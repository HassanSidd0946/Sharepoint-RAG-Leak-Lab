# FINDINGS

Verified API behavior and open questions. Every entry in "Verified behavior" needs: date,
endpoint, what we expected, what actually happened, and a doc link if any.

## Verified behavior

### Driver app: certificate app-only auth works (Graph and SharePoint)
- Date: 2026-10-15
- Endpoint: `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, client credentials
  with certificate, scopes `https://graph.microsoft.com/.default` and
  `https://{SP_HOST}/.default`
- Expected: both tokens issue; the certificate private key (PEM, `.key` file) and a 40-hex
  SHA-1 thumbprint are accepted by MSAL.
- Actual: both tokens issued. `appid` equals `LEAKLAB_DRIVER_CLIENT_ID`; `tid` equals
  `LEAKLAB_ALLOWED_TENANT_ID`, so the `assert_test_tenant()` guard passed on real tokens.
  Closes the thumbprint-format question in `auth.py`.
- Probe output: probes/out/token_probe.json (`probes/token_probe.py`)
- Docs: n/a

### Driver app token claims: roles and audiences
- Date: 2026-10-15
- Endpoint: same as above (claims decoded without signature verification)
- Expected (from DAY0_DAY1_RUNBOOK.md lines 48-50): Graph token carries `User.ReadWrite.All`,
  `Group.ReadWrite.All`, `Sites.FullControl.All`, `Files.ReadWrite.All`; SharePoint token
  carries `Sites.FullControl.All`.
- Actual:
  - Graph token: `aud` = `https://graph.microsoft.com`, `roles` = exactly those four.
  - SharePoint token: `aud` = `00000003-0000-0ff1-ce00-000000000000` (the bare SharePoint
    Online application ID; no `/{host}@{tid}` suffix), `roles` = `["Sites.FullControl.All"]`.
  - Both: `iss` = `https://sts.windows.net/{tid}/` (v1-style issuer).
  - The roles were granted and admin-consented: they appear in the tokens.
- Not yet verified: that SharePoint REST accepts this SharePoint token (needs a first REST call).
- Probe output: probes/out/token_probe.json
- Docs: n/a

### Graph connectivity: GET /organization works with the app-only token
- Date: 2026-10-15
- Endpoint: `GET https://graph.microsoft.com/v1.0/organization` (app-only Graph token)
- Expected: possibly HTTP 403, because `Organization.Read.All` is not among the driver app's
  roles.
- Actual: HTTP 200; `displayName` = `UCP`. The token's roles were exactly
  `User.ReadWrite.All`, `Group.ReadWrite.All`, `Files.ReadWrite.All`, `Sites.FullControl.All`,
  so one of these (or another grant) is sufficient for this endpoint. We did not test which.
- Re-run of the claims check: Graph roles and SharePoint audience/roles identical to the
  entry above, `tid` guard passed again.
- Probe output: probes/out/token_probe.json
- Docs: n/a

### Nested Entra groups: transitiveMemberOf resolves 3 levels, but reads are eventually consistent
- Date: 2026-10-15
- Endpoint: `GET /v1.0/users/{id}/transitiveMemberOf`; group nesting via
  `POST /v1.0/groups/{id}/members/$ref`; `POST /v1.0/users`, `POST /v1.0/groups`
- Expected: after nesting inner -> mid -> outer and adding probe-a to inner, probe-a's
  transitiveMemberOf lists all three groups, and probe-b (in no group) lists none of them.
- Actual (a single run, `probes/graph_probe.py`; timings are from the terminal output):
  - Final state matched the expectation: all three groups appeared; probe-b saw none.
  - Reads were NOT monotonic. Poll 1 (t=0.3s after the last write): 2 of 3 groups
    (inner, mid). Poll 2 (t=5.6s): 0 of 3. Poll 3 (t=11.6s): 3 of 3. So a membership
    result that was visible once disappeared on the next read before settling.
  - "All three visible" took 11.6s from the last membership write. One run only: the spread is
    unknown.
- Implications: one transitiveMemberOf read is not a reliable answer in the first seconds
  after a change. Any check that decides access from it must either wait and re-read until it
  is stable, or fail closed on a missing or changed answer. The scenario runner must not treat
  the first read after a mutation as ground truth.
- Caveat on evidence: the raw poll data (`total_returned` per poll) was lost, because the
  `--cleanup` run overwrote `probes/out/graph_probe.json`. The numbers above come from the
  terminal output only. The probe now writes cleanup results to `graph_probe_cleanup.json`.
  Re-run the probe to get the poll data on disk (and a second timing sample).
- Probe output: terminal output only (see caveat)
- Docs: n/a

### Entra object creation: newly created groups are not immediately addable as members
- Date: 2026-10-15
- Endpoint: `POST /v1.0/groups/{id}/members/$ref` right after `POST /v1.0/groups`
- Expected: unknown (marked UNVERIFIED in `graph_probe.py`).
- Actual: the first nesting call (inner into mid) returned HTTP 404 twice, with 5s between
  attempts, and succeeded on the third attempt (about 10s after the groups were created). The
  next two adds (mid into outer, probe-a into inner) succeeded on the first attempt.
  The 404 body was not recorded.
- Implication: the generator must retry 404 on member adds after creating objects. It is not
  an error.
- Probe output: terminal output only (see caveat above)
- Docs: n/a

### Tenant setup facts confirmed by the probe
- Date: 2026-10-15
- The tenant's domain `leaklabdev.onmicrosoft.com` is valid: users were created as
  `ll-probe-a@leaklabdev.onmicrosoft.com` and `ll-probe-b@...`.
- Creating users with the `LEAKLAB_PERSONA_PASSWORD` value was accepted (it meets the tenant's
  password policy), and `POST /users`, `POST /groups` and the app roles
  `User.ReadWrite.All` / `Group.ReadWrite.All` are sufficient for it.
- `DELETE /users/{id}` and `DELETE /groups/{id}` succeeded for all five probe objects via
  `--cleanup`; objects were found by exact name and prefix `ll-probe-`.
- Probe output: probes/out/graph_probe_cleanup.json (delete report)

### Persona sign-in with a nonexistent user fails with AADSTS50034 (no device-code fallback)
- Date: 2026-10-15
- Endpoint: token endpoint, username/password (ROPC) via `auth.get_persona_token`
- Expected: a clear failure for a user that is not in the directory.
- Actual: `invalid_grant`, `error_codes [50034]` ("user account does not exist in the
  directory"). `needs_interaction()` correctly treated it as not interaction-required, so
  there was no device-code fallback. The UPN used was the placeholder
  `p-intern@YOURTENANT.onmicrosoft.com`, so this says nothing yet about the persona app or
  about MFA/consent behavior.
- Probe output: probes/out/token_probe.json (`error` field)
- Docs: n/a

<!-- Entry template:
### <short title>
- Date:
- Endpoint:
- Expected:
- Actual:
- Probe output: probes/out/<name>.json
- Docs:
-->

## Open questions

- [ ] Persona sign-in (username/password): persona users do not exist yet (generator not
  written), so the persona app, public-client-flow setting, MFA/consent codes
  (`INTERACTION_ERROR_CODES` in `auth.py`) and `preferred_username`/`upn` claims are untested.
- [ ] `tenant_spec.yaml` `naming.domain` is still the placeholder `YOURTENANT.onmicrosoft.com`;
  the real domain is `leaklabdev.onmicrosoft.com` (verified above). Update the spec before
  running the generator or persona probes.
- [ ] How long can transitiveMemberOf stay inconsistent, and does it flap? One sample only
  (11.6s, with a 2 -> 0 -> 3 sequence). Re-run `graph_probe.py` a few times; also check how the
  SharePoint/Azure AI Search side sees the same change.
- [ ] Adding a user/group that is already a member: the assumed error (HTTP 400, "already
  exist") is still unverified; no run hit it. Also unverified: re-creating a deleted probe
  user's UPN straight after `--cleanup`.
- [ ] Does SharePoint REST accept the app-only SharePoint token whose `aud` has no
  `/{host}@{tid}` suffix?

- [ ] How to break permission inheritance on a folder/file: Graph may not support it directly;
  SharePoint REST `breakroleinheritance` likely needed (requires certificate app-only auth).
- [ ] How Entra security groups appear as SharePoint principals (claims format) and how to grant
  them access via REST role assignments.
- [ ] Whether Graph `driveItem` permissions show inherited vs. unique grants, and how.
- [ ] How SharePoint groups (site Owners/Members/Visitors) and special claims such as
  "Everyone except external users" appear in permission responses.
- [ ] What happens to a file's unique permissions when it is moved to another folder.
- [ ] Whether unlicensed users can be granted access and be trimmed correctly in Azure AI Search.
- [ ] Which token audience/scope the Azure AI Search ACL query requires for the end user.
