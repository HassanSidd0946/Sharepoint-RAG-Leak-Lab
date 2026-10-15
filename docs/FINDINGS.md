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
- SharePoint REST acceptance of this token: verified on 2026-10-15, see "SharePoint REST
  works with the app-only token" below.
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

### Graph calls used by GraphClient (users, groups, membership, lookups)
- Date: 2026-10-15
- Endpoints, each returned the expected status in `graph_probe.py` / `sp_probe.py`:
  - `GET /users/{upn}?$select=...`: 404 for a missing user (so `get_user_by_upn` -> None), 200 for
    an existing one
  - `POST /users` (201), `DELETE /users/{id}` (204)
  - `POST /groups` with `securityEnabled=true, mailEnabled=false, groupTypes=[]` (201)
  - `GET /groups?$filter=displayName eq '<name>'&$select=id,displayName`: works with no
    ConsistencyLevel header; empty `value` when missing; `DELETE /groups/{id}` (204)
  - `POST /groups/{id}/members/$ref` with `{"@odata.id": ".../directoryObjects/{id}"}` for both a
    user and a group as the member (204), nested to 3 levels
  - `GET /users/{id}/transitiveMemberOf?$select=id,displayName` (200; see consistency note
    above)
  - `GET /sites/{host}:/sites/LeakLab` and `GET /sites/{id}/drive` (200)
- Not exercised for real: paging (`@odata.nextLink`), the retry path, and adding an existing
  member (see Open questions).
- Probe output: probes/out/sp_probe.json, probes/out/graph_probe_cleanup.json
- Docs: n/a

### Tenant setup facts confirmed by the probe
- Date: 2026-10-15
- The tenant's domain `leaklabdev.onmicrosoft.com` is valid: users were created as
  `ll-probe-a@leaklabdev.onmicrosoft.com` and `ll-probe-b@...`. The site is
  `https://leaklabdev.sharepoint.com/sites/LeakLab`. `tenant_spec.yaml` now holds both.
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

### SharePoint REST works with the app-only token (no form digest needed)
- Date: 2026-10-15
- Endpoint: `GET {site}/_api/web?$select=Title,Url`, and POSTs under `{site}/_api/...`, with
  `Authorization: Bearer <app-only SharePoint token>` and
  `Accept: application/json;odata=nometadata`
- Expected: unknown. The token's `aud` is the bare `00000003-0000-0ff1-ce00-000000000000`
  (no `/{host}@{tid}`), and POSTs might have needed an `X-RequestDigest`.
- Actual: GET returned 200 (`Title=LeakLab`). All POSTs (`breakroleinheritance`, `ensureuser`,
  `addroleassignment`) returned 200 with no `X-RequestDigest` header. With `odata=nometadata`,
  the two POSTs that return nothing answered `{"odata.null": true}`.
- Probe output: probes/out/sp_probe.json (`probes/sp_probe.py`)
- Docs: n/a

### Breaking inheritance on a folder (REST): copyRoleAssignments=false empties it, files follow
- Date: 2026-10-15
- Endpoint: `POST .../GetFolderByServerRelativeUrl('<url>')/ListItemAllFields/
  breakroleinheritance(copyRoleAssignments=false,clearSubscopes=true)`
- Expected: folder gets unique permissions with no copied assignments; the file inside keeps
  inheriting, now from the folder.
- Actual:
  - Before: folder and file both `HasUniqueRoleAssignments=false`, and REST roleassignments
    were the site's three groups: LeakLab Owners (Full Control), LeakLab Members (Edit),
    LeakLab Visitors (Read).
  - After the break and one added assignment: folder `HasUniqueRoleAssignments=true` with ONLY
    the added group (Read); the three site groups are gone (nothing was copied). The file stays
    `HasUniqueRoleAssignments=false` and its REST roleassignments equal the folder's (the added
    group only).
  - Not observed: the state between the break and the added assignment (no read in between).
    The folder then had no assignments from our side; a generator that breaks inheritance
    should add the grants immediately and not leave that window open longer than needed.
  - Not tested: behavior of children that already have unique permissions (`clearSubscopes`).
- Probe output: probes/out/sp_probe.json
- Docs: n/a

### Entra security group as a SharePoint principal: claim `c:0t.c|tenant|<group-object-id>` works
- Date: 2026-10-15
- Endpoint: `POST /_api/web/ensureuser {"logonName": "c:0t.c|tenant|<entra-group-id>"}`, then
  `POST <item>/roleassignments/addroleassignment(principalid=10,roledefid=1073741826)`
- Expected: unknown (this claim format was a guess, marked UNVERIFIED).
- Actual: ensureuser returned 200: `Id=10`, `LoginName` = the same claim, `Title` = the group's
  display name, `PrincipalType=4` (security group). The Read assignment then appeared in REST
  roleassignments with `Member.PrincipalType=4`. The group had been created via Graph about
  25s earlier (the probe waited 20s first). Whether it works with no wait is untested.
- Role definitions (ids are this tenant's, found by name, not hardcoded): Full Control
  1073741829 (RoleTypeKind 5), Design 1073741828 (4), Edit 1073741830 (6), Contribute
  1073741827 (3), Read 1073741826 (2), Limited Access 1073741825 (1). Exactly one role is named
  "Read".
- Probe output: probes/out/sp_probe.json
- Docs: n/a

### How Graph represents permissions (driveItem `/permissions`)
- Date: 2026-10-15
- Endpoint: `GET /v1.0/drives/{drive}/items/{id}/permissions` on a file and its folder, before
  and after the change above
- Expected: inherited entries distinguishable from unique ones.
- Actual:
  - Role mapping in `roles`: SharePoint Full Control -> `owner`, Edit -> `write`, Read ->
    `read`.
  - Principals: a SharePoint group appears as `grantedToV2.siteGroup` (`id` = SharePoint group
    id, e.g. "3") plus `sharePointGroup`. An Entra group appears as `grantedToV2.group` (`id` =
    Entra object id) plus `siteUser` (`id` "10", `loginName` = the claim). A user appears as
    `grantedToV2.user` (`id` = Entra object id) plus `siteUser` (`loginName`
    `i:0#.f|membership|<upn>`). The legacy `grantedTo` is also present.
  - Each entry's `id` is the unpadded base64url of the principal's SharePoint `LoginName`
    (decoded and checked for a site group, the Entra group claim and a user).
  - Inherited vs. unique is NOT reliably visible. Before the change, every entry on both the
    folder and the file carried `inheritedFrom: {}` (empty), though REST says both inherit from
    the site root. After the break, entries on both the folder (unique) and the file
    (inheriting from the folder) have no `inheritedFrom` key at all. Use REST
    `HasUniqueRoleAssignments` to tell the two apart.
  - Graph listed the user Hassan Siddiqui (`owner`, siteUser id 6) separately on the folder and
    file, before AND after the break, although REST roleassignments never list him directly
    (before: only the three site groups; after: only the new group). He is a member of LeakLab
    Owners and is the drive owner. Why Graph lists him is not verified (a site collection
    admin is the likely cause). This is a permission LISTING, not an access test.
  - Graph showed the change on the next read after it was made (folder: 4 entries -> 2). One
    sample only.
- Implication for the oracle: an admin-like account may keep access to a folder with unique
  permissions, which differs from a model of "only the listed grants apply". Decide the rule
  deliberately (and test the admin's real access) before relying on it; see Open questions.
- Probe output: probes/out/sp_probe.json
- Docs: n/a

### SharePoint site groups and default site-root permissions
- Date: 2026-10-15
- Endpoint: `GET /_api/web/associated{owner,member,visitor}group`,
  `GET /_api/web/sitegroups/getbyid(<id>)/users`
- Actual: LeakLab Owners Id 3, Members Id 5, Visitors Id 4 (`PrincipalType` 8). Default
  assignments at the site root: Owners Full Control, Members Edit, Visitors Read. Owners has one
  member, the admin user (`PrincipalType` 1, `LoginName` `i:0#.f|membership|<upn>`). Members and
  Visitors are empty. No "Everyone except external users" claim appears in any of these.
- Note: the spec's `site.root_grants` (grp-all-staff Read) is not applied yet; the generator
  must do that, and these default groups exist alongside it.
- Probe output: probes/out/sp_probe.json
- Docs: n/a

### File and folder creation via Graph; library URL mapping
- Date: 2026-10-15
- Endpoint: `POST /drives/{drive}/root/children`, `PUT /drives/{drive}/items/{folder}:/{name}:/content`,
  `GET /drives/{drive}/root:/{name}`, `DELETE /drives/{drive}/items/{id}`
- Actual: folder created (201) and a text file uploaded (201) with the app-only Graph token; the
  file was readable through REST right away. A lookup of a missing folder returned 404 with
  code `itemNotFound`. The Graph drive is named "Documents" but its URL path is
  `/sites/LeakLab/Shared Documents`; the REST server-relative URL built from the drive's
  `webUrl` path (percent-decoded, then re-encoded) worked. `DELETE` of the folder returned 204
  (it goes to the recycle bin, not purged).
- Probe output: probes/out/sp_probe.json, probes/out/sp_probe_cleanup.json
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
- [ ] The retry policy (`http_retry.py`: 429/503/504, Retry-After, backoff) is only unit-tested
  with mocks; no real throttling response has been seen yet.
- [ ] How long can transitiveMemberOf stay inconsistent, and does it flap? One sample only
  (11.6s, with a 2 -> 0 -> 3 sequence). Re-run `graph_probe.py` a few times; also check how the
  SharePoint/Azure AI Search side sees the same change.
- [ ] Adding a user/group that is already a member: the assumed error (HTTP 400, "already
  exist") is still unverified; no run hit it. Also unverified: re-creating a deleted probe
  user's UPN straight after `--cleanup`.
- [ ] Does the admin (Hassan Siddiqui) keep real access to a folder with unique permissions?
  Graph still lists him as `owner` there, REST does not list him. Test with the admin's own
  token (a delegated read of a file in a unique-permission folder), then decide the oracle rule.
- [ ] Does SharePoint honor NESTED Entra group membership for access (grant on a group, user in
  a group nested inside it)? Needed for scenario S2. Not tested; sp_probe granted a group
  with no members.
- [ ] Does `ensureuser` work on a brand-new Entra group with no wait? The probe waited 20s.
- [ ] Graph breaking inheritance: not tried (REST works, so low priority). Children with their
  own unique permissions under `clearSubscopes=true`: not tested.
- [ ] Special claims such as "Everyone except external users" and org-wide sharing links: not
  observed anywhere yet; the site has none. Needs a probe that grants one and reads it back.
- [ ] How long Graph and REST permission reads lag after a change (one Graph sample was
  immediate), and whether the Azure AI Search ACL view lags longer.
- [ ] What happens to a file's unique permissions when it is moved to another folder.
- [ ] Whether unlicensed users can be granted access and be trimmed correctly in Azure AI Search.
- [ ] Which token audience/scope the Azure AI Search ACL query requires for the end user.
