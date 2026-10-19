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

### GET /subscribedSkus is denied with the driver app's four roles
- Date: 2026-10-15
- Endpoint: `GET https://graph.microsoft.com/v1.0/subscribedSkus` (app-only token whose roles are
  `User.ReadWrite.All`, `Group.ReadWrite.All`, `Files.ReadWrite.All`, `Sites.FullControl.All`)
- Expected: unknown (marked UNVERIFIED in `graph_client.py`).
- Actual: HTTP 403 `Authorization_RequestDenied` ("Insufficient privileges to complete the
  operation"). `persona_probe.py` stopped there; nothing was created.
- Fix (per Microsoft's docs, not yet tried): grant the application permission
  `LicenseAssignment.Read.All` (least privileged; `Directory.Read.All` and
  `Organization.Read.All` also qualify) to the driver app and grant admin consent. For
  `POST /users/{id}/assignLicense` the docs list `User.ReadWrite.All` as sufficient, which the
  app already has.
- Resolved: the driver app was granted `LicenseAssignment.Read.All` (application) and the same call
  then returned 200 (one SKU). Confirmed by `token_probe.py` on 2026-10-15: the Graph app token's
  roles are now `User.ReadWrite.All`, `LicenseAssignment.Read.All`, `Group.ReadWrite.All`,
  `Files.ReadWrite.All`, `Sites.FullControl.All`. The SharePoint token is unchanged
  (`Sites.FullControl.All`, same audience), the tenant guard passed and `/organization` returned
  200 again. The grant matches what Microsoft's docs list as least privileged for this call.
- Probe output: probes/out/persona_probe.json (`error` field, first run)
- Docs: https://learn.microsoft.com/en-us/graph/api/subscribedsku-list?view=graph-rest-1.0 ,
  https://learn.microsoft.com/en-us/graph/api/user-assignlicense?view=graph-rest-1.0

### Persona sign-in works end to end (username/password, no MFA, no propagation errors)
- Date: 2026-10-15
- Endpoint: token endpoint, ROPC via `auth.get_persona_token(..., allow_device_code=False)`, scope
  `https://graph.microsoft.com/User.Read`; then `GET /v1.0/me`
- Expected: possibly MFA / consent / public-client errors, or "user not found yet" errors right
  after creation (the probe was set to retry 50034 and 50126).
- Actual: both users (`ll-p-intern`, `ll-p-plateng`, created seconds earlier with the persona
  password and `usageLocation`) signed in on the FIRST attempt, 12.6s and 9.4s after the probe
  created them (that time includes the license calls). No error code appeared for either user.
  So in this tenant: the persona app allows public-client flows, delegated consent is in place
  and security defaults / Conditional Access did not block ROPC.
- Token: `aud` = `https://graph.microsoft.com`, `appid` = the persona client id, `scp` = `Files.Read.All
  GroupMember.Read.All Sites.Read.All User.Read profile openid email`, `upn` and `oid` present and
  equal to the created user, v1-style issuer. `GET /me` returned 200 with the same id and UPN for
  both. The `upn` claim is therefore usable for `auth._verified`.
- Not exercised: the retry path (no error occurred) and the MFA / consent error codes.
- Probe output: probes/out/persona_probe.json
- Docs: n/a

### Licenses: SKU discovery works; assignedLicenses did not show the license right away
- Date: 2026-10-15
- Endpoint: `GET /v1.0/subscribedSkus`, `POST /v1.0/users/{id}/assignLicense`,
  `GET /v1.0/users/{id}?$select=id,usageLocation,assignedLicenses`
- Actual: the tenant has exactly one SKU, `O365_BUSINESS_ESSENTIALS` (appliesTo `User`, `Enabled`,
  25 enabled, 1 consumed before the run), so auto-selection worked without a hardcoded id.
  `POST /users` with `usageLocation` and `assignLicense` both succeeded (HTTP 200) for both users.
  However, `assignedLicenses` read straight after the call was EMPTY for both users. Whether and
  when it fills in was not observed (the users were deleted afterwards).
- Probe output: probes/out/persona_probe.json (`assigned_sku_ids_after`)
- Docs: https://learn.microsoft.com/en-us/graph/api/user-assignlicense?view=graph-rest-1.0

### Real read access: nested Entra groups are honored, an outsider is denied (403), the admin keeps access
- Date: 2026-10-15
- Endpoint: `GET /v1.0/drives/{drive}/items/{file}/content` with DELEGATED tokens (scope
  `Files.Read.All`, persona app), on a folder whose inheritance was broken
  (`copyRoleAssignments=false`) and whose ONLY grant is Read for group OUTER (REST roleassignments
  confirmed: `ll-probe-outer -> ['Read']`). Groups: inner nested in mid nested in outer.
- Expected: p-plateng (in inner) allowed only if SharePoint honors nested groups; p-intern
  (in no probe group) denied; admin to be verified; driver app (app-only control) allowed.
- Actual (3 rounds, 10s apart, 12 reads; seconds since the grant: first read 94.5s, last 183.2s):
  - p-plateng: 200 and the canary in the body, 3 of 3. SharePoint honored THREE-LEVEL nested
    Entra group membership for access.
  - p-intern: 403 with Graph error code `accessDenied`, no redirect, canary absent, 3 of 3.
    A denial is 403 here (not 404).
  - admin: 200 and the canary, 3 of 3, although REST lists only OUTER on the folder. So the
    admin keeps access to a folder with unique permissions (this explains why Graph listed
    the admin as `owner`). WHY is unverified (site collection admin vs. LeakLab Owners member vs.
    something else).
  - driver app (app-only, control): 200 and the canary, 3 of 3, so the file existed and the denial
    of p-intern is a real access decision.
  - No flapping in any subject across the 3 rounds.
- Mechanics: an allowed read answers 302 with a pre-authenticated download URL on
  `*.sharepoint.com` (followed without the Authorization header, never stored); a denied read
  answers 403 straight away.
- Limits: the earliest read was 94.5s after the grant (the admin device-code sign-in sits
  in between), so this says nothing about how soon a new grant takes effect, nor about how soon a
  REMOVED grant or membership takes effect. p-plateng's token was issued AFTER its membership was
  stable; a token issued before a membership change was not tested.
- Implication for the oracle: nested group expansion is correct, and "unique permissions mean only
  the listed grants" is correct for ordinary users. The admin account is an exception and needs an
  explicit oracle rule (e.g. exclude the admin from leak assertions, or model it as always
  allowed).
- Probe output: probes/out/access_probe.json (`reads`, `summary`)
- Docs: n/a

### Admin delegated sign-in by device code works
- Date: 2026-10-15
- Endpoint: device code flow on the persona app, scope `Files.Read.All`, admin account
  (`auth.get_persona_token(..., use_password=False)`)
- Actual: the device code was issued, the admin completed it in a browser, and the resulting token
  passed the "issued to the requested UPN" check and read the file. The admin's password was never
  used by the probe.
- Probe output: probes/out/access_probe.json

### Second sample: transitiveMemberOf lag, license visibility, re-creating deleted UPNs
- Date: 2026-10-15
- transitiveMemberOf (`access_probe`, p-plateng added to inner nested in mid nested in outer,
  groups created minutes earlier): 0 of 3 groups at 0.4s and 6.0s, 3 of 3 at 12.3s and 17.8s. No
  flapping this time (the earlier run saw 2 -> 0 -> 3), but again about 12s until the full
  membership was visible. The negative control (p-intern) saw none, consistently.
- Nesting a brand-new group: the first add (inner into mid) returned HTTP 404 once and succeeded
  after 5s (earlier run: twice). `ensureuser` on a group created in the same run worked with no
  explicit wait (a gap of tens of seconds; the exact gap was not measured).
- assignedLicenses: after `assignLicense`, the license was visible after 0.6s for p-intern and
  12.7s for p-plateng (in `persona_probe` an immediate read had still been empty). So a lag of
  up to about 13s is possible; two samples.
- The same UPNs (`ll-p-intern`, `ll-p-plateng`), deleted by `persona_probe --cleanup`
  earlier in the session, were created again without error (state `created`). Re-creation
  "straight away" after a delete was not tested; a gap of at least minutes was.
- Probe output: probes/out/access_probe.json

### Site-root grant reaches inheriting folders (sp_probe root-grant)
- Date: 2026-10-15
- Endpoint: `POST /_api/web/roleassignments/addroleassignment(principalid=<id>,roledefid=<Read>)`;
  reads: `GET /_api/web/roleassignments?$expand=Member,RoleDefinitionBindings`,
  `GET /_api/web?$select=HasUniqueRoleAssignments`, and Graph `/permissions` of an inheriting folder
- Expected: unknown (marked UNVERIFIED).
- Actual: HTTP 200, `{"odata.null": true}`. The site root is its own permission scope
  (`HasUniqueRoleAssignments=True`). Before: LeakLab Members (Edit), Owners (Full Control),
  Visitors (Read), plus two "Limited Access" principals (see the next entry). After: the same plus
  the probe group with Read. A new folder (`HasUniqueRoleAssignments=False`) listed exactly the same
  assignments before and after, including the new group. Graph's `/permissions` of that folder
  listed the group (`read`) alongside Owners, Members, Visitors and the admin, 5 entries. The other
  root assignments were unchanged. Both REST and Graph showed the grant on the first read after it
  (seconds); one sample.
- Cleanup (`removeroleassignment` at the site root) was run later and worked, see "Cleanup facts".
- Probe output: probes/out/sp_probe_root-grant.json
- Docs: n/a

### "Limited Access" entries appear on their own and outlive deleted groups
- Date: 2026-10-15
- Observed (REST roleassignments of the site root and of inheriting items):
  - Granting a USER on a single file (file-grant) made the parent folder, which inherits from the
    site root, list `ll-probe-a -> Limited Access` although nothing was granted on the folder.
    SharePoint adds "Limited Access" for a principal at the parent scope(s) of a uniquely-granted
    item.
  - Two entries, `ll-probe-inner` and `ll-probe-outer` with Limited Access, were already on the site
    root before this run. They are left over from earlier probes whose Entra groups have since been
    deleted, so deleting the Entra group does NOT remove its SharePoint principal or its Limited
    Access assignment.
  - Graph `/permissions` did NOT list those Limited Access principals (5 entries where REST listed 8).
- Implications (facts to keep in mind, not decisions): an entry in REST `roleassignments` is not
  proof of read access (Limited Access does not grant reading content); teardown of the test tenant
  leaves orphaned principals and Limited Access entries unless they are removed explicitly.
- Probe output: probes/out/sp_probe_root-grant.json, probes/out/sp_probe_file-grant.json

### Unique permissions on a single FILE, granted to a user; the real user claim (sp_probe file-grant)
- Date: 2026-10-15
- Endpoint: `POST <file ListItemAllFields>/breakroleinheritance(...)`, `POST /_api/web/ensureuser`
  with the PLAIN UPN `ll-probe-a@leaklabdev.onmicrosoft.com`, then `addroleassignment`
- Actual: break inheritance on a file worked (200). `ensureuser` accepted the plain UPN and returned
  `LoginName` = `i:0#.f|membership|ll-probe-a@leaklabdev.onmicrosoft.com` (lowercase UPN),
  `PrincipalType` 1, `Title` = `ll-probe-a`, `UserPrincipalName` set, `Email` empty. So the assumed
  claim format is verified for a user (it is also what the admin has). After the grant, the file had
  `HasUniqueRoleAssignments=True` and exactly one assignment (the user, Read); Graph listed the user
  (`read`) and the admin (`owner`). The containing folder was untouched (`HasUniqueRoleAssignments=
  False`, but it now listed the user with Limited Access, see above).
- Probe output: probes/out/sp_probe_file-grant.json (`claim_check`)
- Docs: n/a

### Moving a uniquely-permissioned file keeps its own permissions (sp_probe move)
- Date: 2026-10-15
- Endpoint: Graph `PATCH /v1.0/drives/{drive}/items/{file-id}` with
  `{"parentReference": {"id": "<destination-folder-id>"}}`; REST reads before and after
- Setup: source folder (inherits the site root) with a file that has unique permissions (user:
  Read); destination folder with unique permissions (a different group: Read).
- Actual: the PATCH returned 200 and the item id did not change. After the move the file was still
  `HasUniqueRoleAssignments=True` with exactly its own assignment (the user, Read). It did NOT take
  the destination folder's assignments (the group). Graph `/permissions` listed the same two
  principals before and after (the user and the admin).
- Caveats: this is the permission LISTING; no user read was attempted after the move. One sample.
- Implication: for scenario S4 (`f-direct-share` moved into a restricted folder), the listing says
  the file keeps its own grants; the destination folder's grants are not applied to it.
- Probe output: probes/out/sp_probe_move.json
- Docs: n/a

### ensureuser on a brand-new Entra group fails with HTTP 400 for a variable time
- Date: 2026-10-15
- Endpoint: `POST /_api/web/ensureuser` with `c:0t.c|tenant|<group-object-id>`
- Actual: `400`, `odata.error.code` `-2146232832, Microsoft.SharePoint.SPException`, message "The
  specified user c:0t.c|tenant|<id> could not be found." Not a 404. Observations: succeeded when the
  group had been created about 15s earlier (root-grant) and in earlier probes after 20s or more;
  failed for a group created about 15-20s earlier (nested-folder, group A) and for a group created a
  few seconds earlier (idempotency). So there is no fixed delay and no verified upper bound.
  `sp_probe` now retries this one error every 5s up to 120s and records attempts and seconds.
- Probe output: probes/out/sp_probe_nested-folder.json, probes/out/sp_probe_idempotency.json
  (the failed runs)
- Docs: n/a

### Unique child folder under a unique parent (sp_probe nested-folder)
- Date: 2026-10-15
- Endpoint: `breakroleinheritance(copyRoleAssignments=false,clearSubscopes=true)` on a parent and
  then on its child folder, `addroleassignment`, REST and Graph reads
- Setup: parent folder: unique, Read for group A. New child folder created inside it. File inside the
  child. Then the child: unique, Read for group B.
- Actual:
  - A new child of a unique parent inherits the PARENT's unique assignments
    (`HasUniqueRoleAssignments=False`, lists group A with Read).
  - After the child's own break and grant: child `HasUniqueRoleAssignments=True` with ONLY group B
    (Read). Group A is not listed on the child. A file in the child has
    `HasUniqueRoleAssignments=False` and lists only group B.
  - The parent (unique) lists group A (Read) and also `ll-probe-nest-b -> Limited Access`. Granting B
    on the child made SharePoint add Limited Access for B on the nearest parent scope (same
    mechanism as in the "Limited Access" entry above). The probe's own check "parent has only A"
    therefore printed False; the real role on the parent is A's Read only.
  - Graph `/permissions` of the child listed B (`read`) and the admin (`owner`).
- Caveat: listings only; no user reads were attempted.
- Probe output: probes/out/sp_probe_nested-folder.json
- Docs: n/a

### Idempotency: repeating a role assignment is harmless; repeating a group-member add is a 400
- Date: 2026-10-15
- Endpoints: `POST <item>/roleassignments/addroleassignment(...)` twice with the same arguments;
  `POST /v1.0/groups/{id}/members/$ref` twice for the same user and for the same group
- Actual:
  - Role assignment: both calls returned 200 with `{"odata.null": true}`. The folder listed the group
    once (no duplicate assignment).
  - Group member (user and group alike): first add 204; second add HTTP 400, code
    `Request_BadRequest`, message "One or more added object references already exist for the
    following modified properties: 'members'." This is exactly what `GraphClient._add_member`
    assumed, so that assumption is now verified.
- Probe output: probes/out/sp_probe_idempotency.json (`evaluation`)
- Docs: n/a

### Cleanup facts: removeroleassignment works; Graph deletes lag behind GETs
- Date: 2026-10-15
- `POST /web/roleassignments/removeroleassignment(principalid,roledefid)` on the site root returned 200
  and the assignment was gone on re-read (8 -> 7 assignments). The probe group was deleted only after
  that, so no orphan was left for it. The 7 remaining assignments are the three site groups and
  Limited Access entries of earlier probe principals (not removed).
- A user deleted by one cleanup was still returned by `GET /users/{upn}` seconds later, and
  `DELETE /users/{id}` then answered 404 `Request_ResourceNotFound`. Deletes lag behind lookups, so
  cleanup code must treat a 404 on delete as "already gone" (`sp_probe` now does).
- Re-running `nested-folder` and `idempotency` after cleanup: `ensureuser` on fresh groups succeeded on
  the first attempt every time (0.4s to 1.6s), including groups created seconds-to-15s earlier, so the
  earlier 400s remain unexplained variance.
- Probe output: probes/out/sp_probe_root-grant-cleanup.json, sp_probe_file-grant-cleanup.json,
  sp_probe_nested-folder.json, sp_probe_idempotency.json

### Revocation: reads stayed ALLOWED for 15+ minutes after the group membership was removed
- Date: 2026-10-16
- Probe: `probes/revocation_probe.py` (direct mode: p-plateng is a direct member of the group that holds
  the folder's only grant). Every read is a real one: Graph `GET /drives/{d}/items/{id}/content` with a
  delegated token for p-plateng, 200 only if the canary is in the body.
- Endpoint for the removal: `DELETE /v1.0/groups/{group}/members/{user}/$ref` (returned success in about
  1s both times).
- Expected: reads turn 403 within seconds to minutes after the removal; a token fetched after the
  removal is denied no later than one fetched before it.
- Actual (2 trials; numbers are from the terminal output, because the raw JSON was overwritten by a
  failed follow-up run):
  - New member access: the first 200 read came 16.7s (trial 1, new group) and 10.5s (trial 2) after
    the add. Group membership as seen by Graph was stable after about 6-12s.
  - After the removal, the polling ran for about 15 minutes per trial (poll timeout 900s). EVERY read
    was allowed: trial 1: 172 allowed / 0 denied / 0 errors for the old token and 172/0/0 for a fresh
    token (fetched 2.9s after the removal); trial 2: 160/0/0 for both (fresh token at 3.1s). No
    denial, no flapping. App-only control reads were allowed before and after, so the file existed.
  - Each polling cycle took about 5.2s (two tokens, two requests each), not the requested 2s, so the
    time resolution is about 5s.
  - The removal did take effect in Entra at some point: trial 2's re-add (about 15 minutes after
    trial 1's removal) was reported as `added`, not "already a member". The probe did NOT check at the
    time when Entra stopped listing the user (it does now: `entra_checks` in the output).
- What this shows: for at least about 15 minutes after the removal call returned, a user who is no
  longer in the group that holds the only grant could still read the file, with an old token and with
  a token issued after the removal. Contrast: p-intern, who was never a member, was denied on every
  read in `access_probe`; the delay is in REVOKING a previously valid member.
- What this does NOT show (open): the cause (candidates, none verified: SharePoint caching of the
  user's Entra group membership; a cache in the Graph content path; the removal reaching Entra later
  than the probe could see); whether and when the read ever turns 403 (no upper bound known);
  nested removal (S2 as in the spec); removal of the role assignment instead of the membership (S1);
  other read paths (SharePoint REST with a SharePoint-audience token, browser access).
- Why it matters: this is SharePoint's own behavior, which sits below Azure AI Search. A harness that
  treats "current SharePoint read access" as instant ground truth would be wrong by at least this
  window, and a native index built on SharePoint permissions can be no fresher than what SharePoint
  itself enforces. No oracle decision has been taken from this.
- Process note: a `--mode nested` run was started without cleaning up the direct run's folder, stopped
  on "already exists" (as designed), and overwrote `revocation_probe.json`. The probe now writes a
  per-variant file and never overwrites a successful result with a failed one.
- Probe output: terminal output (direct run); probes/out/revocation_probe.json currently holds only
  the failed nested attempt.
- Docs: n/a

### A newly added member got 403 on every read for 5+ minutes (variable, unlike the earlier runs)
- Date: 2026-10-16
- Probe: `probes/revocation_probe.py --trials 1 --poll-timeout 1800`, direct mode, right after
  `--cleanup` (so the group, folder and grant were created from scratch).
- Setup facts: `ensureuser` on the new group needed one retry ("not found yet", 5s) although the probe
  had waited 15s after creating the group. Graph's `transitiveMemberOf` showed p-plateng in the group
  from about 11s after the add (0/1, 0/1, 1/1, 1/1 at 0.3s, 6.1s, 11.4s, 17.0s).
- Actual: with a token acquired after that, EVERY read was denied (403) for 306s after the add: 42
  reads, about 7s apart, no 200. The probe stopped at its 300s access timeout. Whether the read would
  ever have succeeded is unknown.
- Contrast: in the earlier direct run (also a freshly created group in trial 1, then re-adding the
  same user in trial 2) the first 200 came 16.7s and 10.5s after the add. So the delay before a NEW
  member can read varies from about 10s to more than 5 minutes between runs of the same probe.
- Together with the revocation result (access persisted 15+ minutes after removal), SharePoint's view
  of group membership lagged the directory in BOTH directions, by large and variable amounts, with
  fresh tokens each time. The cause is not established; nothing here is a verified explanation.
- Data note: this run's 42 read results were not saved (a stopped trial was not stored); they exist
  only in the terminal output. The probe now stores a trial as soon as it starts and records, for each
  read while waiting, whether Entra lists the member.
- Probe output: terminal output only (probes/out/revocation_probe.json has the setup, no trial data)
- Docs: n/a

### A new member was still denied after 30 minutes while Entra listed them as a member
- Date: 2026-10-16
- Probe: `probes/revocation_probe.py --trials 1 --access-timeout 1800 --poll-timeout 1800`, direct
  mode, right after `--cleanup`; same user (p-plateng), a freshly created group, folder and grant.
- Actual (saved in probes/out/revocation_probe.json): Graph showed the user in the group stably from
  17.0s after the add, and `ensureuser` resolved the group on the first attempt (0.4s). Then 218
  reads, from 21.2s to 1804.7s after the add, were ALL denied: HTTP 403, code `accessDenied`, never a
  200. For every one of those reads Entra listed the user as a member. The probe stopped at its
  1800s access timeout.
- The three direct-mode runs so far, all with p-plateng and a freshly created group:

  | Run | New member's first 200 | After removal |
  |---|---|---|
  | A (2 trials) | 16.7s and 10.5s after the add | still allowed for 15+ minutes, old and fresh token |
  | B | none in 306s (stopped) | not reached |
  | C | none in 1805s (stopped), Entra lists member | not reached |

  What changed between A and B/C is not in the code path that talks to SharePoint (later changes only
  added Entra checks, saved data and options). A ran first; B and C ran after it, on new groups.
- HYPOTHESIS (not verified; later NOT supported by the brand-new-user run, see the next entry): SharePoint keeps its own per-user
  view of the user's Entra group membership, fixes it at the user's first access (or when it
  expires) and keeps it for a long time (30+ minutes). That would explain all three runs: in A the
  user's view was built fresh at the first read, so the new group was included (200 in seconds), and
  then it stayed frozen (the removal was not reflected for 15+ minutes; the re-add made no
  difference); in B and C the frozen view came from run A and did not include the NEW group (a
  new object id), so every read was denied although Entra lists the membership. Earlier probes fit
  too: p-plateng read through nested groups on its first access, shortly after being created.
- A test that can refute it: `--throwaway-user NAME` runs the same trial with a brand-new user that has
  never touched SharePoint. The hypothesis predicts a first 200 within about 10-20s for that user
  and, after a removal, access that persists; a brand-new user that is ALSO denied for a long time
  would contradict it. Other explanations are not excluded (something about the new group's
  SharePoint principal or grant).
- Why it matters if it holds: SharePoint's own enforcement can be stale in both directions, and by
  how much would depend on when each user last accessed it, not on when the permission changed.
- Probe output: probes/out/revocation_probe.json (run C, status `failed` because it stopped at the
  timeout; the data are complete)
- Docs: n/a

### A brand-new user was also denied (the "per-user view fixed at first access" hypothesis is NOT supported)
- Date: 2026-10-16
- Probe: `probes/revocation_probe.py --throwaway-user rev1 --trials 1` (default access timeout 300s),
  right after `--cleanup`: a new licensed user `ll-probe-rev1` (created 15s before), a new group and
  grant, and the same folder/file names as the earlier runs.
- Actual: Graph showed the user in the group stably after about 11s. About 34 reads, from 18.2s to
  305.4s after the add, were ALL denied (403); Entra listed the member on each one. The probe stopped
  at its 300s timeout.
- The previous entry's hypothesis predicted a quick 200 for a user who had never touched SharePoint.
  That did not happen, so the hypothesis is not supported by this run. Caveat: a brand-new user may
  also be waiting for its SharePoint licence/profile to provision, so this run alone does not prove
  the hypothesis wrong in every form.
- A confound that the earlier runs share and nobody tested: the only run in which a new member could
  read within seconds (run A) was the first time the probe's FOLDER and FILE names
  (`ll-sp-revoc-folder`, `ll-revoc.txt`) were used. Runs B, C and this one each re-created that
  folder and file right after deleting them (the deleted ones sit in the recycle bin). The
  earlier `access_probe`, which also worked, used a new folder name but re-used group names from
  `graph_probe`, so group-name reuse alone does not obviously break access.
- Status: no explanation is established. Candidates not yet separated: re-use of a deleted folder
  path; something about the time of day or service state; the licence/profile state of the user.
- (Done in the next entry: new names worked.) Next experiment (now supported by the probe): `--suffix NAME` gives the folder, file and groups new
  names; every read cycle also records SharePoint's own effective-permission answer for the user
  (`GET .../getusereffectivepermissions`, UNVERIFIED endpoint), which shows whether SharePoint itself
  thinks the user can read, independently of the Graph read path.
- Probe output: probes/out/revocation_probe.json (status `failed`: it stopped at the timeout; the
  data are complete)
- Docs: n/a

### With brand-new object names a new member reads in 22.8s; removal still not honored for 30 minutes
- Date: 2026-10-16
- Probe: `probes/revocation_probe.py --suffix n1 --trials 1 --access-timeout 1800 --poll-timeout 1800`
  (direct mode, p-plateng reused, NEW folder, file and group names: `...-n1`). Complete data in
  probes/out/revocation_probe.json (status ok).
- Adding the member: Graph showed the user in the group stably after about 17s; the first real read
  was allowed 22.8s after the add (HTTP 302 then 200 with the canary). At the same moment SharePoint's
  own effective-permission answer was "can read".
- Removing the member (`DELETE .../members/{user}/$ref`, 0.39s): Entra reported the user removed at
  the second check, 36.8s after the call (the first check, at 6.1s, still listed the user). Then, for
  1800.8s (30 minutes), 208 cycles: every read with the OLD token and every read with a FRESH token
  (fetched 2.8s after the removal) was allowed (208/0/0 each, no denial, no flapping); app-only
  control reads allowed before and after. A read cycle took 8.7s, so the resolution is about 9s.
- SharePoint's own view agrees: `getusereffectivepermissions` for the user on the folder returned
  `Low = 138612833` on all 208 cycles, exactly the Read role's `BasePermissions.Low` seen earlier.
  So the continued access is not an artifact of the Graph content path; SharePoint itself still
  treated the user as having Read 30 minutes after Entra had dropped the membership (within 37s).
  (The endpoint is now verified: it works on a folder's list item with the app-only token.)
- Which of the earlier observations this explains (candidates, not proof):
  - New object names worked twice (runs A and n1: first 200 in 10-23s, even for the long-used user
    p-plateng); re-created names failed three times (B, C, D: no 200 in 306s, 1805s and 305s, the last
    with a brand-new user). That makes re-use of the deleted FOLDER PATH (and file name) the leading
    candidate for the "new member denied" runs; the per-user explanation is out. The mechanism is
    unknown. `access_probe` worked while re-using only group names (from graph_probe), so group-name
    reuse alone does not look responsible. Run afterwards (see "Re-created names" below): the same suffix twice in a row (cleanup, then
    re-create the same names), which would test this directly.
  - Revocation: two independent setups (run A, 15+ minutes; n1, 30 minutes) show the same thing, now
    confirmed by SharePoint's own effective-permission answer.
- Working practice, not a rule about SharePoint: until the name-reuse question is settled, give every
  generated folder, file and group a name not used before in the tenant (a run suffix), and expect
  teardown/regenerate cycles that re-create the same paths to be unreliable.
- Probe output: probes/out/revocation_probe.json
- Docs: n/a

### Re-created names (same suffix twice): SharePoint reported NO permissions for the member for 7+ minutes
- Date: 2026-10-16
- Probe: `--cleanup --suffix n1`, then `probes/revocation_probe.py --suffix n1 --trials 1 --access-timeout 600`:
  the same folder, file and group names as the previous successful run, re-created minutes after
  they were deleted. (The data are in probes/out/revocation_probe.json, status ok.)
- Adding the member: Graph showed the user in the group stably after about 19s. 43 reads from 33.5s
  to 452.1s after the add: 41 were HTTP 404 `itemNotFound`, 1 was a 302 whose download then failed
  with `accessDenied`, and the LAST one (452.1s) was allowed. SharePoint's own effective-permission
  answer for the user was `Low = 0` (no permissions at all) on all 43 reads, and Entra listed the user
  as a member on all of them. (The same names, used the first time, had given a first 200 at 22.8s
  and `Low = 138612833`, the Read mask, from the start.)
- After that single allowed read the probe removed the member (a design flaw, fixed below). Entra
  dropped the membership by 9.5s. Over the next 183s (21 cycles) reads FLAPPED: old token 10 allowed /
  11 denied, fresh token 11 allowed / 10 denied, and the two tokens disagreed within the same cycle
  (e.g. at t=25.0s old allowed, fresh denied; at t=74.6s old denied, fresh allowed). Some "denied"
  results were a 302 from Graph followed by `accessDenied` at download, others `404 itemNotFound`.
  SharePoint's effective-permission answer stayed `Low = 0` throughout, so reads succeeded
  intermittently while SharePoint's own query said "no permissions".
- NOT a valid revocation measurement: the baseline was one 200 followed immediately by the removal,
  and polling stopped early (at 183s) because the old token had three denials in a row although the
  fresh one had already flipped back to allowed. Treat the post-removal numbers as an observation of
  inconsistency only. The probe now requires `--baseline-reads` (default 3) allowed reads in a row
  before removing, polls until EVERY token has 10 denials in a row, and stores each read's final
  (download) status.
- Tally of the first access of a newly added member, same user (p-plateng) except where noted:
  brand-new names: 16.7s and 10.5s (run A), 22.8s (n1, first time), 2 of 2 within 23s; re-created
  names: no 200 in 306s, none in 1805s, none in 305s (brand-new user), 452s with flapping afterwards
  (n1, second time), 4 of 4 delayed or failed. Small numbers, not randomized, each re-created run
  followed a cleanup by minutes: strongly suggestive of an effect of RE-CREATING deleted names, not
  proof, and the mechanism is unknown.
- What can be said regardless of the explanation: (1) SharePoint's effective permissions can say
  "no permissions" for a user that Entra lists in a group holding the only grant, for many minutes;
  (2) reads for the same user at the same moment can come out differently (two tokens; and a Graph
  302 that SharePoint then refuses); (3) neither is explained by token age. A guess, not a finding:
  more than one server may be answering with different cached state.
- Probe output: probes/out/revocation_probe.json
- Docs: n/a

### Brand-new names did NOT help: reads were coin flips (name reuse is not the explanation)
- Date: 2026-10-16
- Probe: `probes/revocation_probe.py --suffix a1 --trials 1 --access-timeout 600`, direct mode,
  p-plateng, a NEW folder, file and group (`...-a1`, never used before), `--baseline-reads 3`.
  Complete data in probes/out/revocation_probe.json (status ok).
- Adding the member: Graph showed the user in the group stably after 29s. Reads while waiting (21 reads,
  32s to 211s after the add): 14 `404 itemNotFound` from Graph, 1 302 that ended in `403 accessDenied`
  at the download, 6 allowed; allowed and denied alternated (first allowed read at 194.6s, the
  baseline of 3 allowed in a row only at 211.3s). SharePoint's effective-permission answer was "no
  permissions" until the last of those reads (211.3s) and "can read" (`Low = 138612833`) from then on.
- After the removal (Entra dropped the membership by 8.4s): 152 cycles over 903s. Old token: 71
  allowed / 81 denied; fresh token (fetched 5.0s after removal): 81 allowed / 71 denied. The old
  token's answer flipped on 70 of 151 consecutive cycles (46%), the fresh token's on 69. In a cycle
  the two tokens agreed on only about half the cycles: (old, fresh) = (denied, allowed) 42, (denied,
  denied) 39, (allowed, allowed) 39, (allowed, denied) 32, i.e. roughly independent coin flips with
  about a 50% chance of "allowed" per read. Of the 304 reads over both tokens: 152 allowed (`302`
  then `200`), 84 `404 itemNotFound` from Graph, and 68 a `302` whose download ended in
  `403 accessDenied`. SharePoint's effective-permission answer said "can read"
  on all 152 cycles, i.e. it kept saying so for 15 minutes after the removal. App-only control reads
  were allowed.
- So the earlier explanation that RE-USING names causes the trouble is not supported: this run used
  names never used before and showed the same delayed, flapping behavior as the re-created-names run.
  The two clean runs (A and n1-first: every read allowed, effective permission "can read" from the
  start) remain unexplained.
- Regimes seen so far for the same user in the same tenant, direct-mode runs: every read allowed
  (2 runs); every read denied for 5-30 minutes (3 runs); roughly half of the reads allowed, at random
  (2 runs: n1-second and this one). Revocation could only be assessed in the first regime, and there
  it did not happen for 15-30 minutes.
- What this means for measurement: a single read is not a reliable "has access / has no access"
  signal in this tenant. Any harness that decides leak or no-leak from one read needs repeated
  reads, and even then the regime matters. Nothing here is a decision about the oracle.
- Probe output: probes/out/revocation_probe.json
- Docs: n/a

### Steady state, two arms (c1): a direct grant was 100% allowed, an Entra-group grant 0% for 10 minutes
- Date: 2026-10-16 (run at 11:26 UTC; the saved file is probes/out/consistency_probe.json, status ok).
  This was the first, two-arm version of `probes/consistency_probe.py`. Its results were not
  reported when first asked about, and an earlier summary wrongly said the probe had not been run.
- Setup: p-plateng (reused); folder A with Read granted DIRECTLY to the user; folder B with Read
  granted to a new Entra security group that contains the user (Graph showed the membership stable
  after 13.0s). One token, nothing changing, 113 read cycles over 600s, plus SharePoint's own
  effective-permission query on each folder.
- Result: direct grant: 113/113 allowed, 0 flips, effective permission "can read" 100%. Entra-group
  grant: 0/113 allowed (every read `404 itemNotFound`), 0 flips, effective permission "cannot read"
  100%, for 10 minutes while Entra listed the user in the group.
- A second run of the same version (c2, brand-new user `cons2`) failed on a network error while
  signing in (a read timeout to login.microsoftonline.com) and produced no data
  (probes/out/consistency_probe.failed.json).
- Probe output: probes/out/consistency_probe.json
- Docs: n/a

### Three arms (c1 again): direct and SharePoint-group grants revoke in about 16s; an Entra-group grant persisted 35+ minutes
- Date: 2026-10-16 (run at 12:06 UTC; probes/out/consistency_probe_run_20261016T120649Z.json, status
  `failed` because a network error ended the run at 39.2 of 45 minutes; the data up to then are
  saved).
- Setup: p-plateng; three folders with broken inheritance, one canary file each, one grant each:
  DIRECT (the user), SPGROUP (a new SharePoint group containing the user), ENTRA (a new Entra
  group containing the user). The three new SharePoint-group calls worked (create, add user,
  remove user, find, grant).
- Phase 1, steady state, 71 cycles over 605s, one token: all three arms 100% allowed, 0 flips,
  SharePoint effective permission "can read" 100%, first run of 5 allowed reads at 12.2s.
- Phase 2, revocation: the user was removed from all three arms back to back (Entra 1.44s,
  SharePoint group 0.36s, direct assignment 0.41s, 1.8s from first call to last). A token acquired
  seconds before (pre-t0) and one acquired right after (fresh, 0.4s) read every arm every cycle (a
  cycle took about 13s, so "first deny" is accurate to about 16s). 179 cycles, up to 2353s:

  | Arm | Pre-t0 token | Fresh token | SharePoint effective permission |
  |---|---|---|---|
  | DIRECT | first deny 16.1s; 179/179 denied, 0 allowed | same | "cannot read" from 16.1s on |
  | SPGROUP | first deny 16.5s; 179/179 denied, 0 allowed | same | "cannot read" from 16.5s on |
  | ENTRA | **174 of 179 allowed**; first deny 2175.5s (36.3 min), 5 denials, no run of 10 | **175 of 179 allowed**; first deny 2137.0s (35.6 min), 4 denials | **"can read" on all 179 cycles** |

  After the first denials the Entra arm flipped (7 and 6 flips) and was still allowed on most reads
  until the run ended. 36 structure checks (every 60s) showed the removals in effect everywhere from
  the first check at 15.7s: Entra did not list the user, the SharePoint group did not contain the
  user, and the direct assignment was gone.
- The run stopped on `ConnectTimeout` (an SSL handshake) at about 2354s, and the `--cleanup` that
  followed failed on the same error, so the folders and groups of this run still need cleaning up.
  The probe now treats a network error during a read as an `error` read and keeps polling.
- Probe output: probes/out/consistency_probe_run_20261016T120649Z.json
- Docs: n/a

### Three arms replicated with a brand-new user: Entra-group revocation again took about 36 minutes
- Date: 2026-10-16 (run at 13:05 UTC; probes/out/consistency_probe_run_20261016T130544Z.json, status
  ok, the full 45 minutes of phase 2).
- Setup: a NEW licensed user (`ll-probe-cons3`, created minutes earlier), new names (`-c3`), the same
  three arms as before (DIRECT, SPGROUP, ENTRA). `ensureuser` on the new Entra group needed two
  retries (10s). Phase 1: 67 cycles over 600s: all three arms 100% allowed, 0 flips, SharePoint
  effective permission "can read" 100%, first run of 5 allowed from 10.0s.
- Phase 2 (195 cycles, 2711s; removals 0.95s, 0.33s and 0.36s, within 1.3s of each other):

  | Arm | Pre-t0 token | Fresh token |
  |---|---|---|
  | DIRECT | first deny 14.3s, 10 in a row from 14.3s, 0 allowed, 194 denied (+1 network-error read) | first deny 14.3s, 0 allowed, 195 denied |
  | SPGROUP | first deny 14.6s, 0 allowed, 195 denied | same |
  | ENTRA | first deny **2162.4s (36.0 min)**, 179 allowed / 16 denied | first deny **2128.4s (35.5 min)**, 183 allowed / 12 denied |

  After its first denials the Entra arm kept flipping: 27-44% of reads were denied in the 12 minutes
  that followed (e.g. by 120s window: 2/16, 9/20, 2/14, 4/12, 7/14, 4/12 denied), never 10 in a row,
  so it had NOT settled when the 45 minutes ended. SharePoint's own effective-permission answer for
  the Entra arm was "can read" on all 195 cycles (up to 45 minutes after the removal). The structure
  checks (every 60s) showed the removal in effect in all three arms from the first check at 13.8s.
- Compared with the first three-arm run (p-plateng, `-c1`): first denial 35.6-36.3 min there, 35.5-36.0
  min here; both tokens each time; in both runs the denials began at almost the same moment for the
  token acquired before the removal and for the one acquired after it, so the token is not what
  holds access open. Two runs, two different users, two sets of names, the same lag to within a minute.
  The earlier runs also fit (15 and 30 minutes of uninterrupted access, no denial before 36 minutes).
- One transport error (`ConnectError`) hit a read on the direct arm at about 1120s; it was recorded as
  an `error` read and polling continued (this would have ended the previous run).
- Both cleanups worked: three folders (204 each), the SharePoint group, the Entra group and the
  throwaway user were all removed without error (`-c1` and `-c3`).
- Probe output: probes/out/consistency_probe_run_20261016T130544Z.json
- Docs: n/a

### Three arms with a 120s steady state: the Entra arm never had access, so there was nothing to revoke
- Date: 2026-10-16 (run at 14:38 UTC; probes/out/consistency_probe_run_20261016T143812Z.json, status
  ok, ended early after about 2.5 minutes of phase 2).
- Purpose: test whether the 36-minute Entra lag depends on how long the user had access before the
  removal (`--phase1 120` instead of 600), with a brand-new user (`ll-probe-dd1`) and new names (`-d1`).
- Result, phase 1 (13 reads per arm): DIRECT and SPGROUP 100% allowed, 0 flips, SharePoint effective
  permission "can read" 100%. ENTRA: 0 of 13 allowed (denied from the first read at 12.2s through
  122s), SharePoint effective permission "cannot read" 100%, while Graph listed the user in the group
  stably from 17s.
- Result, phase 2: all six arm-and-token series were denied from the first cycle (DIRECT 11.0s,
  SPGROUP 11.4s, ENTRA 11.8s), 0 allowed, and the run stopped early. The ENTRA numbers mean nothing:
  that arm had no access before the removal, so the experiment did NOT test the question.
- What it does add: another run in which an Entra-group grant gave a brand-new user no access at all
  (as in the two-arm run), next to runs where the same recipe gave immediate, clean access (the
  `-c1` and `-c3` three-arm runs). Tally of the Entra-group arm for a user freshly added to a new group
  and read from about 12s on (11 runs so far): immediate access 4 (A, n1 first use, three-arm `-c1`,
  `-c3`); no access for the whole observation (2 to 30 minutes) 5 (B, C, D, two-arm `-c1`, `-d1`);
  delayed or flapping 2 (n1 second use, a1). The direct and SharePoint-group arms were fine in every
  run they were in (steady state in four runs, revocation in three).
- Probe change: the probe now checks, before the removal, that each arm showed 5 allowed reads in a
  row, waits up to `--baseline-wait` (300s) for arms that did not, and flags an arm without a
  baseline as "NO (nothing to revoke)" in the results and the output file (`valid_arms`), excluding it
  from the stop condition. Without it this run would have looked like a valid revocation result.
- Probe output: probes/out/consistency_probe_run_20261016T143812Z.json
- Docs: n/a

### What the two probes (and the earlier runs) say together: the instability is tied to Entra-group grants
- Every unstable observation so far involved a grant to an ENTRA security group: new members denied
  for 5-30 minutes (runs B, C, D, c1 group arm, n1-second), coin-flip reads (n1-second, a1), and
  revocation not honored for 15-39 minutes (run A, n1-first, the ENTRA arm above).
- DIRECT user grants and SharePoint-group membership never showed any instability: two steady-state
  runs (113 and 71 cycles, 100% allowed, no flips), and one revocation that took effect within
  about 16s on both, with SharePoint's own effective permission agreeing.
- The Entra-group arm itself was NOT always bad: in the three-arm run its steady state was 100%
  allowed from the first read, like the other arms, and it was only the revocation that lagged (36
  minutes before the first denial, with SharePoint's own effective permission still "can read" at 39
  minutes). In the two-arm run, 40 minutes earlier with the same user and the same setup recipe, the
  Entra-group arm was 100% denied for 10 minutes. So for Entra groups both the start and the end of
  access are unpredictable (regimes: fast and clean; denied; coin flips), while the other two grant
  types were predictable.
- Limits: few runs (two steady-state, one revocation for direct and SharePoint-group grants), one
  user, one tenant, one day. The SharePoint-group arm contained the user directly (not an Entra
  group inside a SharePoint group). "No instability seen" is not proof of stability. No mechanism is
  established; a guess consistent with the numbers (not verified) is that SharePoint keeps its own
  cached view of a user's Entra group membership, updated lazily (the first denials came after
  about 36 minutes), while direct assignments and SharePoint-group membership are evaluated from
  SharePoint's own data without it.
- Probe output: probes/out/consistency_probe.json, probes/out/consistency_probe_run_20261016T120649Z.json

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

## Oracle rules supported by verified results (NOT decisions)

This list says what the evidence so far supports. Nothing here has been adopted in
`src/leaklab/oracle/`; the oracle is still unwritten, and any rule it adopts must be chosen
deliberately and explained here.

Supported by real runs:
1. **Nested groups expand.** A grant to a group reaches members of groups nested inside it, three
   levels deep, for real file reads (access_probe: p-plateng read through inner -> mid -> outer).
2. **Unique permissions list only their own grants.** After `breakroleinheritance` with
   `copyRoleAssignments=false`, REST shows exactly the assignments added afterwards, and an ordinary
   user outside them is denied (p-intern: 403 `accessDenied`).
3. **Items without unique permissions follow their parent.** A file under a unique folder has
   `HasUniqueRoleAssignments=false` and shows the folder's assignments (sp_probe).
4. **Allow and deny are observable through Graph.** Allowed: 200 with the file content (via a 302 to
   a pre-authenticated URL). Denied: 403 `accessDenied`, no redirect. A canary in the body is
   therefore a usable leak signal for reads as a given user.
5. **Principal forms seen:** an Entra security group via claim `c:0t.c|tenant|<group-object-id>`
   (PrincipalType 4); site groups (PrincipalType 8); the admin user as `i:0#.f|membership|<upn>`
   (PrincipalType 1).

6. **Grants at the site root are inherited** by folders and files that do not have unique
   permissions (root-grant: REST and Graph both listed the group on an inheriting folder).
7. **A file keeps its own unique permissions when moved** into a folder with different unique
   permissions (move; listing only, not a read test).
8. **A user is a principal via `i:0#.f|membership|<upn lowercase>`**; `ensureuser` resolves a plain
   UPN to it (file-grant).
9. **Listing caveat:** REST `roleassignments` also contains "Limited Access" entries that are not read
   access (and may belong to deleted principals); Graph `/permissions` omits them. A comparison with
   the oracle must not count Limited Access as access.

10. **A unique child scope replaces its parent's grants.** A child that breaks inheritance lists
    only its own grants (the parent's group is not listed on it), and items inside it follow the
    child (nested-folder; listing only).
11. **Repeating a role assignment does not change the result** (no duplicate, no error).

Observed but not settled (needs a decision, or more evidence, before it becomes a rule):
- **The admin account keeps access** to a unique-permission folder it is not listed on. The cause
  (site collection admin vs. Owners-group member) is unverified, so there is no rule yet for which
  accounts this applies to. Options for later: exclude the admin from leak assertions, or model it
  as always allowed.
- **Grant type matters.** Direct user grants and SharePoint-group membership took effect, and were
  revoked, within about 16s; Entra-group grants showed delayed starts, flapping reads and a revocation
  that was not honored for 35+ minutes (see the three-arm entries). Any oracle that assumes one
  propagation behavior for all grant types is wrong for this tenant.
- **Timing.** Membership became visible after about 12s in two samples, a license after up to
  about 13s, and a new member could read after 10-23s with brand-new object names but still got 403
  after 5-30 minutes in three runs that re-created deleted folder/file/group names (Entra listed the
  member throughout). Removing a member was NOT reflected in real
  reads, nor in SharePoint's own effective-permission answer, for at least 30 minutes (old and fresh
  tokens alike; two setups; Entra had dropped the membership within 37s; see "Revocation"). So
  "who can read right now in SharePoint" lags the directory state by a window whose upper bound is
  unknown. The oracle should not encode a time window until that bound is measured, and the question
  of what the oracle should compare against (directory state vs. real reads) is open.
- **Licensing.** Only licensed personas were tested for access.

Still not probed: real READS for the cases above that were only listed (nested folders, moved
file, file-level grant), and everything on revocation timing.

## Oracle implementation notes (src/leaklab/oracle/)

What the oracle encodes, and what it deliberately does not. Everything encoded comes from
"Oracle rules supported by verified results"; the choices below are implementation decisions, listed
so they can be challenged.

Encoded (each has unit tests and, where it is a general rule, a Hypothesis property checked against
an independent brute-force reference):
- Nested Entra-group expansion (any depth); a user grant; roles `read`, `edit` and `full_control` count
  as read access, `limited_access` and any unknown role do not.
- Unique permissions list only their own grants; inheriting items take their parent's effective grants;
  top-level folders take the site-root grants.
- A moved special file keeps its own grants; one without its own grants follows the folder it is in.
- Only users with `persona: true` are evaluated. Site admins are NOT evaluated (the open decision about
  the admin account is not guessed).
- Fail closed: an unknown principal kind never matches (and is logged); a mutation the oracle cannot
  interpret (unknown folder, group or file, grant on an item that inherits, breaking inheritance on the
  root) raises OracleError instead of being ignored.

Implementation decisions to review:
- Intent changes at `confirmed_at`. A mutation with `confirmed_at = None` has no effect. Mutations with
  the same time apply in list order.
- A grant mutation on an item that still inherits is an error (break inheritance first), because that is
  what SharePoint needs; `inheritance_broken` without `copy_role_assignments` leaves the item with no
  grants (verified: nothing is copied).
- A membership cycle created by a mutation is tolerated (the expansion terminates). Whether Entra would
  allow such a cycle is not known.
- The oracle gives the SAME answer for every grant type. It does NOT model the observed difference
  between grant types: direct and SharePoint-group changes took effect within about 16s, Entra-group
  changes took about 36 minutes to be revoked and were sometimes not applied at all. That difference
  belongs to what SYSTEMS do, not to what access SHOULD be; the scoring module measures it as time to
  conformance, per system, instead of baking a delay into the oracle.
- Scoring: LEAK (served while intent denies; wins over LOCKOUT), LOCKOUT (withheld while intent allows),
  OK. Reads in the window right after an intent change count, so the propagation lag shows up as leak
  or lockout reads and as time_to_conformance. Convergence = N consecutive conforming reads (N
  configurable, default 10) that last to the end of the segment (`require_stable_tail`, default on), with
  an optional cap; a segment shorter than N reads is INSUFFICIENT_DATA, not a failure.
- Because single reads were unreliable in this tenant (coin-flip regime), N consecutive reads and the
  stable-tail rule are what keep a lucky streak from being counted as convergence.

Not decided / not implemented: the admin account's access; any time window for "should have caught up";
Limited Access as a separate state; Setup A's own view (the oracle has none).

## Generator notes (src/leaklab/generator/)

Status: implemented, tested against an in-memory fake tenant (tests/generator_fakes.py), and run LIVE once
(2026-10-16, `uv run pytest --live tests/test_generator_live.py`, 1 passed in 254s):
- generate: 9 users created and 2 reused (ll-p-intern and ll-p-plateng already existed), 7 groups, 17
  memberships (12 user and 5 group-in-group, as in the spec), 7 folders, 37 documents, inheritance broken
  on 7 items (6 folders and the special file) with 9 grants added back to back, 1 site-root grant.
- verify (REST for structure, Graph for membership) reported NO issue, not even a warning, so the site's
  Members and Visitors groups were empty.
- a second generate created and changed nothing (11 users, 7 groups, 7 folders, 37 files reused, 17
  memberships "already"), i.e. it is idempotent on the real tenant.
- teardown removed the root assignment, the container, 7 groups and all 11 users (including the two
  that existed before: they are part of the spec). The two persona probes users must be recreated by
  the next run that needs them.
Not exercised by that run (so only fake-tested at the time): the retry paths (no event showed whether
they triggered), license assignment for NEWLY created users, apply_mutation and the mutation log against
the real tenant, and drift handling. The mutation run below covers all but drift handling.

### SharePoint groups, the S1 control arms, and a changing user id (2026-10-17)
Spec additions: principal kind `sp_group`; `sp_groups:` (members are users only: an Entra group inside a
SharePoint group is not verified, so the oracle raises OracleError and the generator raises
GeneratorError for it); Entra group `grp-s1-entra`; folders `S1-Entra`, `S1-SPGroup`, `S1-Direct` (2
documents each, p-plateng reads them through an Entra group, a SharePoint group and a direct grant);
scenarios `S1-entra`, `S1-spgroup`, `S1-direct` (remove p-plateng from the group / from the group / remove
the grant), all with control folder `Engineering/Platform-Secrets`. Scenarios now carry `mutations`
(oracle Mutation type + params) and an optional `control`. The tenant is now 11 users, 8 Entra groups,
1 SharePoint group (`ll-sp-s1-spgroup`), 10 folders, 43 documents (42 + the special file).
The generator uses only the SharePoint-group calls consistency_probe verified (create, find by Title,
add user by login claim, list users, remove by user id, delete by id). Still UNVERIFIED: that a SharePoint
group appears in role assignments with `Member.LoginName` equal to the LoginName returned by find (the
gateway assumes LoginName == Title when the create response has none). verify() compares exactly that.

FINDING (live, 2026-10-17, probes/user_grant_400_probe.py, probes/out/user_grant_400_probe_20261017T035625Z.json):
- Expected: the id that `ensureuser` returns for a user can be used for `addroleassignment` later in the run.
- Actual: for ll-p-plateng (freshly created AND freshly licensed) the id cached from an early ensureuser
  (right after the user was created, while filling the SharePoint group) was rejected by
  `addroleassignment` on S1-Direct and on Board: HTTP 400, code `-2147024809, System.ArgumentException`,
  "Value does not fall within the expected range." A later `ensureuser` for the same UPN returned a
  DIFFERENT id (77) which worked; ll-p-ceo and ll-p-intern, ensured late, worked on the first try.
  The earlier live generator run did not hit this because users were only ensured at the permissions
  step. Cause unknown: the id may be replaced when the license is applied or the user is first synced;
  untested.
- Decision: `Env.principal` never caches a USER principal (Entra groups and SharePoint groups are still
  cached; groups showed no such problem). Regression test:
  `test_a_user_id_that_changes_between_lookups_is_never_reused` (fails with the cache restored).
- Not fixed: a user's id could still change between the `ensureuser` and the `addroleassignment` that
  follows it milliseconds later; never seen.

### apply_mutation on the real tenant (2026-10-16, probes/mutation_probe.py)
Result file: probes/out/mutation_probe_20261016T165809Z.json. Fresh generate (11 users, 7 groups, 17
members, 7 folders, 37 files created) + verify: OK, no issue. Then three mutations, one at a time, each
re-checked with the probe's own REST/Entra reads (not the check inside apply_mutation); teardown at the
end removed everything (1 root assignment, the container, 7 groups, 11 users).
- S4 file_moved (f-direct-share General -> Board): Graph shows the file in Board and gone from General;
  REST: still unique, grants unchanged (ceo and hr, Read). Oracle: no change for any persona (the file keeps
  its own grants, as the oracle rule says), so S4 changes WHERE the file lives, not who should read it.
- S1 grant_removed (grp-engineering on Engineering): removed 1 role binding; REST: Engineering has no
  real grant left, still unique, Platform-Secrets still has grp-eng-platform Read. Oracle: p-plateng loses
  the Engineering documents (17 -> 11 canaries); it keeps Platform-Secrets via grp-eng-platform.
- S2 group_member_removed (grp-eng-platform out of grp-engineering): confirmed through witness user
  p-plateng (2 consecutive reads); independent check: p-plateng no longer transitively in
  grp-engineering, still in grp-eng-platform, u-eng1 still in grp-engineering. Oracle: p-plateng loses the
  6 General documents (11 -> 5), i.e. what reached it through grp-engineering -> grp-all-staff.
- confirmed_at was recorded for all three (epoch seconds). Confirmation was immediate for S4 and S1 (first
  check true; confirmed_at is the time of that first check, i.e. the structure showed the change within
  ~1s of the call) and 2.4s after execution for S2. The oracle consumed log.oracle_mutations() and its
  before/after views are in the JSON.
- Retries that fired: member-add 404 (is_member_add_not_ready) 2 times, both while the new groups were
  being filled. The ensureuser "could not be found" retry did NOT fire in this run (so it remains seen
  only in probes and fake-tested). No other retries.
- License assignment for newly created users WORKED: ll-p-intern and ll-p-plateng were both new
  (their earlier copies were deleted by teardown) and Graph showed the SKU on both. The tolerated
  "already assigned" error path was not exercised, and its exact error text is still UNVERIFIED.
- Limits: these confirm STRUCTURE only. Nothing here says when SharePoint starts serving or withholding
  content after each change (that is the scenario runner's job; S1 and S2 revoke Entra-group grants, the
  slow case above). Drift handling and inheritance_restored (unsupported) are still untested live.
- No code fix was needed. The only change made was instrumentation: retries are now recorded as "retry"
  events (retry_call `on_retry`, Env.retry).

Only behaviors recorded above as verified are used:
- Graph: create user (with usageLocation) and group, license via subscribedSkus + assignLicense, member
  add with 404 retry and "already exist" = success, folders and uploads by path, delete with 404 =
  gone, move by PATCH parentReference, transitiveMemberOf.
- SharePoint REST: ensureuser (plain UPN for users, `c:0t.c|tenant|<id>` for groups, retried up to 2
  minutes on HTTP 400 "could not be found"), role definitions by name, HasUniqueRoleAssignments,
  breakroleinheritance(copy=false), add/remove role assignment (items and `/web`), role assignments,
  the site's associated groups. Structure is checked through REST; content is created through Graph.
- Deliberately NOT used because not verified: listing a folder's children, listing a group's direct
  members, resetting inheritance, removing SharePoint site users. verify() therefore checks the
  documents one by one, memberships through each user's transitive groups, and refuses (drift) to
  "fix" an item that should inherit but has its own permissions.

Decisions to review:
- Everything generated lives under one container folder `<prefix>lab`; the spec's folder paths are
  relative to it (the container inherits the site root, so it is transparent to the oracle). Reason:
  the spec's folder names have no prefix, and teardown must delete by prefix only.
- The not-generated `admin` user is neither created nor granted anything (not even the spec's
  root grant for it): it already has Full Control through the Owners group, and a grant to a
  non-prefixed object could not be undone safely. It is listed in Plan.skipped.
- Document file names are `<prefix><folder code>-<nn>.txt`; the text is rendered from
  documents.body_template with a topic and filler derived from the seed (the spec does not define them).
- Permissions: principals are resolved BEFORE inheritance is broken, then inheritance is broken and all
  grants are added back to back. A re-run reconciles a unique item to exactly the spec's grants
  (Limited Access entries are ignored, they are SharePoint's own).
- apply_mutation() supports grant_added/removed, group_member_added/removed, inheritance_broken and
  file_moved, and logs `confirmed_at` (epoch seconds) when Entra/REST first showed the change. It does NOT
  support inheritance_restored (not verified). Confirmation is about STRUCTURE only; what SharePoint
  serves, and when, is for the scenario runner to measure.
- Group-in-group membership changes are confirmed through a witness user (a user the spec puts inside the
  child group); without one, by the call's success alone, and the entry says so.
- Unverified detail kept explicit in the code: the exact error for assigning a license twice (the license
  list lags ~13s behind an assignment; a 400 mentioning "already" is tolerated, anything else raises).
- Not handled: Entra-group grants are known to be enforced by SharePoint with long and unpredictable
  delays (see the consistency results). The generator cannot wait that out; generate() reports
  structure, and verify() says nothing about what SharePoint will serve.
- Teardown leaves SharePoint's orphaned site users and Limited Access entries (removal not verified).
  Teardown also deletes the persona users `ll-p-*` if they exist, because they are part of the spec.
- A delete is not instantly visible to lookups (a user deleted seconds ago can still be returned and then
  404 on use): wait a little between teardown and generate.

## Runner notes (src/leaklab/runner/)

Status (2026-10-17): implemented and tested against the in-memory fake tenant with a fake system that
injects lag, flapping, errors, leaks, a raising adapter and a crash, and run LIVE once (below).

Live runs (2026-10-17): `tests/test_generator_live.py` passed (273 s): 11 users, 8 Entra groups, 1
SharePoint group with 1 member, 10 folders, 43 files created; verify OK, including the SharePoint group's
members and its grant (so the SharePoint group's login did match on re-read: that assumption holds);
a second generate changed nothing; teardown removed 1 SharePoint group, 8 groups, 11 users. It had
FAILED the first time on the changing user id (see "SharePoint groups ... a changing user id").
`tests/test_runner_live.py` passed (332 s): S1-direct through Setup0 with persona tokens
(journal `runs/run_20261017T040826Z.jsonl`, git-ignored): baseline OK, the mutation confirmed, 64 reads,
0 errors, first denial 1.45 s after `confirmed_at`, 0 served reads after it (leak exposure 0.0 s),
converged after 5 conforming reads. ONE run. The earlier probes measured about 11-16 s for direct
grants with a 13 s read cycle and two tokens, so this is not necessarily a different behavior: the
earlier figures were bounded by their own cycle length; a real comparison needs replicates
(`--replicate`) with the same cycle length. Nothing here is a measurement of Azure AI Search or our
overlay, and the slow Entra arm (S1-entra) and the SharePoint-group arm have not been run through the
runner yet.

What it does: `python -m leaklab.runner run --scenario "S1-*" --systems setup0 [--replicate n]
[--fresh] [--resume FILE]` and `... report --run FILE [--json]`. Per scenario: ensure the tenant is
generated and verified, sign the personas in, BASELINE, apply the mutation with
`generator.apply_mutation`, POLL, write a journal and a report. Journals are append-only JSON lines in
`runs/` (git-ignored), one new timestamped file per run.

Definitions (each is a decision to challenge, not a measured fact):
- A read (`Setup0SharePoint`) is the one consistency_probe verified: Graph `GET /drives/{d}/items/{id}/content`
  with the persona's delegated token; 302 -> the pre-authenticated URL (only `*.sharepoint.com`, no
  token) or 403/404. `served` = canary string in the body (exact match). 403/404 and a 200 body without
  the canary are "not served". Everything else (transport error, 401, 429, 5xx, unexpected redirect
  host, sign-in failure) is `served = None`: counted as an error, left out of every number, never a denial.
- Pairs polled per scenario, all derived from the spec and the oracle: FLIPPED (intent changes because
  of the mutation), TARGETED (the first canary of each object the mutation touches, read by every
  persona; intent does not change; they catch a leak or lockout caused by the change itself) and one
  CONTROL (a pair the mutation must not touch; the three S1 arms use `Engineering/Platform-Secrets`
  for p-plateng, one Entra hop, so a control failure is the system's, not the change's).
- BASELINE: every pair needs `baseline_k` (5) conforming reads in a row (errors are skipped, not counted),
  cap 15 min. On the cap the scenario is flagged BASELINE_UNSTABLE and the run CONTINUES.
- POLLING stops when every flipped pair has `n_consecutive` (10) conforming reads in a row AFTER the
  change (the streak from before the change is reset), or `cap_s` (60 min) after `confirmed_at`. A
  scenario that flips nothing (S4) is watched `steady_window_s` (120 s).
- Metrics per (scenario, system), all measured from the mutation's `confirmed_at`:
  `time_to_first_deny_s` (worst revoked pair), `leak_exposure_s` (time of the LAST served read of a
  revoked pair, so a flap extends it), `time_to_conformance_s` (from `oracle.scoring.classify`, stable
  tail) or DID_NOT_CONVERGE / INSUFFICIENT_DATA / NO_DATA / NOT_APPLICABLE / NOT_RUN, `lockout_s` (the
  longest a pair that intent allowed was withheld), `errors`, `baseline_status`, `observations`, and the
  read interval (the resolution of every time). A revoked pair that was NOT being served just before the
  change is listed in `nothing_to_revoke` (the lesson of the d1 run: an arm that never had access proves
  nothing about revocation).
- Scenarios whose pairs do not overlap share ONE polling loop (each unique pair is read once per
  cycle); the others run in later groups, derived from the tenant as the earlier groups left it. A run
  MUTATES the tenant: use `--fresh` (teardown, 60 s settle, generate) for each independent run;
  `--replicate n` does that for runs 2..n.
- Robustness: every read is journaled as it happens; a raising adapter becomes an error read;
  a failed or unconfirmed mutation ends only that scenario (`mutation_failed` / `mutation_unconfirmed`);
  persona tokens are renewed 5 minutes before expiry and a failed sign-in is not retried for 30 s;
  `--resume` replays the journal (observations, mutations, special-file moves) and continues the cycle
  numbering. A crash BETWEEN `mutation_started` and its outcome is refused (the tenant may or may not
  have changed): check it with `generator verify` and start a new run.

### Runner results on the real tenant, Setup0 (2026-10-17)
Journals in `runs/` (git-ignored; the run ids are the evidence). Setup0 = plain SharePoint, persona
tokens, canary-in-body reads. Config: interval 5 s, baseline 5 conforming reads per pair, n = 10, cap 60 min.
- S1-direct and S1-spgroup together, 3 replicates (`run_20261017T042037Z_r1`, `...T042650Z_r2`,
  `...T043258Z_r3`; each on a freshly generated tenant): baseline OK every time; first denial after
  `confirmed_at`: S1-direct 1.8 s, 1.8 s, 1.8 s; S1-spgroup 3.6 s, 3.7 s, 3.9 s; leak exposure 0.0 s (no
  served read after the first denial); 0 errors; verdict OK; converged after 10 conforming reads.
  The runner's resolution is the 5 s read cycle, so "1.8 s" means "denied by the first read after the change".
- S1-entra, 1 run (`run_20261017T043922Z`): baseline OK; mutation = remove p-plateng from `grp-s1-entra`,
  confirmed through the witness user (5.5 s from the call). Both S1-Entra documents were served on
  every read for the first 45 minutes after `confirmed_at` (2 x 270 reads, 0 denials). The first denial
  came at 2734.6 s (45.6 min) for both documents; from then the reads flipped between served and denied
  (document 1: 17 flips, document 2: 13) with a rising denial share: 2/30 and 2/30 reads at 45-50 min,
  5/30 and 2/30 at 50-55, 6/30 and 7/30 at 55-60. The last served read was at 3545 s (59 min). The cap
  (60 min) ended the run: DID_NOT_CONVERGE, verdict LEAK, 0 errors in 1098 reads. The control (p-plateng
  reading `Engineering/Platform-Secrets`, 366 reads) was served every time, and no other persona was ever
  served an S1-Entra document.
- Together with the earlier probes: 3 independent Entra-group revocations, first denial at 35.5, 36.3 and
  45.6 minutes, and in the runs that went on, flapping afterwards. The direct and SharePoint-group arms
  ended within one read cycle. The Entra lag is therefore NOT a constant 36 minutes: the three first
  denials differ by 10 minutes, and this run's denial share was still rising at the cap, so its
  settling time is unknown (more than 60 minutes). The oracle says "deny" from the confirmed change on;
  everything above is the gap between that and what SharePoint served.
- Still open: whether the first denial depends on how long the member was in the group before the removal
  (this run's baseline was only about 1 minute, the earlier probes' 10 minutes or more), on polling
  frequency (5 s here, 13 s before), or on neither; whether the flapping ever settles.

Known limits and decisions to review:
- Polling stops at the first run of 10 conforming reads. The Entra arm flapped (27-44% denied) for 12
  minutes after its first denial; 10 denials in a row by chance then has probability about 0.4^10 =
  1e-4 per attempt, so a false stop is unlikely but not impossible. `--n` raises the bar.
- `apply_mutation` blocks the loop (about 1-3 s; up to 120 s if confirmation is slow): no reads are made
  for that long, and the timestamps show the gap. Other scenarios of the same group are not read during it.
- `confirmed_at` is when Entra/REST SHOWED the change, not when SharePoint serves it. For a revocation
  the oracle therefore already says "deny" while SharePoint may still serve: that interval is exactly
  what `leak_exposure_s` measures, and it is measured from a moment that can lag the real change.
- Resume cannot recover reads that were never made; the polling cap keeps running on wall-clock time
  while the process is down.
- Only Setup0 exists. Setup A (Azure AI Search) and Setup B (the overlay) will implement the same
  `SystemUnderTest` protocol (`read(persona, canary) -> Observation`) and register in `tenant.SYSTEMS`.
- S6 (cached answer) is not a tenant change; `run` refuses it ("no mutations").
- The admin is not evaluated by the oracle, so the runner does not read as the admin either.

Tests (runner): 21 engine, 21 Setup0, 14 scenarios, 6 journal, 9 metrics, 8 CLI, plus 1 live test.
Planted bugs: 23 in setup0/engine/scenarios/metrics/journal. 19 were caught at once. Of the 4
survivors, 2 were real gaps (errors counted as conforming reads during polling; granting scenarios
untested because no spec scenario grants) and now have tests that catch them; 2 were EQUIVALENT mutants
(an error read never reaches the first-deny code; a redundant condition in the control choice, replaced
by a real mutant, "control may be a touched pair", which a new test now catches). So 21 distinct
real bugs are caught, 2 mutants cannot change behavior.

## Setup A: what Microsoft's docs say (read 2026-10-17; NOT verified by us)

Everything here comes from the docs, not from a run. It is the specification the probe
`probes/search_acl_probe.py` tests. Pages: search-indexer-sharepoint-access-control-lists,
search-how-to-index-sharepoint-online, search-query-access-control-rbac-enforcement (learn.microsoft.com/azure/search).
- Preview only: REST api-version `2026-08-01-preview`; incremental ACL updates need `2026-05-01-preview` or
  later (earlier previews capture ACLs only at first ingestion). Basic tier or higher. Not in the portal.
- The indexer needs Entra APPLICATION permissions (delegated is not supported for ACLs): Microsoft Graph
  `Files.Read.All` + `Sites.FullControl.All` (or `Sites.Selected`) with admin consent. Credential in the
  connection string: a client SECRET (`ApplicationSecret=`) or a FEDERATED credential on the search
  service's managed identity (`FederatedCredentialApplicationId=`); a certificate is not one of the options,
  so the driver app (certificate only) cannot be the ingestion app as it is.
- Data source: `type: sharepoint`, `indexerPermissionOptions: ["userIds","groupIds"]`, container
  `useQuery` with `includeFolder=<host>/sites/<site>/<library>/<folder>` (docs example; unverified here).
- Index: fields with `permissionFilter: "userIds"` / `"groupIds"` (Collection(Edm.String), filterable) and
  `permissionFilterOption: "enabled"`; indexer field mappings `metadata_user_ids` -> UserIds,
  `metadata_group_ids` -> GroupIds. The key is `metadata_spo_site_library_item_id` (base64Encode).
- Group types: user ids and Entra group object ids are stored as they are; group nesting is expanded at
  QUERY time through Microsoft Graph (docs), using the user's `oid` from the token. SharePoint site groups
  are NOT handled by the plain configuration; with api 2026-05-01-preview+ they need a federated
  credential, SharePoint API permission `Sites.FullControl.All`, `sharePointConnectorAppRegistration` on the
  index and a `SharePointSiteUrl` field; their ids are then stored as `spg:`-prefixed values in GroupIds. An
  Entra group INSIDE a SharePoint group is not expanded (matches our oracle's refusal).
- Query: caller authorizes with `Authorization: Bearer <app token>` (an RBAC role such as Search Index Data
  Reader) AND the end user's identity travels in `x-ms-query-source-authorization: <user's Entra token>`.
  The audience of that user token is not stated on the pages read (UNVERIFIED; the probe records the answer
  to `https://search.azure.com/.default`). If the user token is omitted, ACL-protected documents are not
  returned. If ACL evaluation fails (e.g. Graph unavailable) the service answers 5xx, never a partial result.
- Debugging: header `x-ms-enable-elevated-read: true` (needs Search Index Data Contributor) returns all
  documents regardless of ACL; set `retrievable: true` on the ACL fields only while investigating.
- Freshness: item-level permission changes on items with UNIQUE permissions are picked up on each indexer
  run (change tokens). A change on a PARENT scope (site, library, list, folder) inherited by children is
  NOT picked up: call `/resync` with `options: ["permissions"]` (then run the indexer) or `/resetdocs`.
  In this tenant the S1 documents INHERIT from a unique folder, so a grant removal on the folder is a
  parent-scope change: the docs predict incremental runs will NOT pick it up. The probe tests exactly that.
- Limits: 1000 permission entries per file; no "Anyone"/org-wide links; no guests.

Not built, deliberately: `src/leaklab/setup_a/`. Order: provision -> ingestion app -> probe stages
(preflight, setup, run, dump, trim, revoke) -> record results here -> only then build.
Blocked on (2026-10-17): the Azure service does not exist yet and the ingestion app/secret is not
configured; the driver app has no Azure role and no app-registration rights (Graph roles: Files.ReadWrite.All,
Group.ReadWrite.All, LicenseAssignment.Read.All, Sites.FullControl.All, User.ReadWrite.All).

### Setup A probes, first results (2026-10-17; probes/out/search_acl_*_20261017T07*.json)
VERIFIED against a real Basic service (`leaklab-search-772918cf`, created with probes/search_provision.py):
- The driver app's Entra token with scope `https://search.azure.com/.default` is accepted as the
  Authorization bearer once the service has `aadOrApiKey` auth and the driver app has the roles
  (Search Service Contributor, Search Index Data Contributor, Search Index Data Reader); api-version
  `2026-08-01-preview` is accepted. Before the roles existed, and against the OTHER service in `.env`
  (`leaklab-search-dev`, which has no roles for the driver app), every call was HTTP 403 with an empty body.
- Data source `type: sharepoint` with `indexerPermissionOptions: [userIds, groupIds]`, a client-secret
  connection string (SharePointOnlineEndpoint = the SITE url) and container `useQuery` was accepted (201).
  `includeFolder=` REQUIRES a full URL with `https://`: the docs' example without the scheme gave
  HTTP 400 "Invalid URI format 'leaklabdev.sharepoint.com/sites/LeakLab/Shared Documents/ll-lab'" at
  indexer creation. With `https://...` the indexer was created (201).
- The indexer ran by itself on creation (an explicit `run` straight after answered HTTP 409 "Another
  indexer invocation is currently in progress"). First run: 07:53:07 -> 07:53:25 (18 s), status success,
  itemsProcessed 43, itemsFailed 0, one warning: "Folder propagation was automatically enabled because
  ACL ingestion is configured. Child items will be re-indexed when folder permissions change. To explicitly
  opt in and suppress this warning, add 'propagateFolderChanges=true' to the indexer query." That CONTRADICTS
  the docs' statement that parent-scope changes are not picked up automatically: to be tested (revoke --rerun).
- OPEN, unexplained: the index reports `documentCount` 36 (still 36 minutes later) although the indexer
  processed 43 items with 0 failures and the tenant has 43 documents. Which 7 are missing is unknown until
  an elevated read or a persona query can list them.
- With the app token only (no user identity), `search=*` returned 0 documents with HTTP 200: protected
  documents are not returned without a user token (as the docs say). All 43 documents are protected here.
- `x-ms-enable-elevated-read: true` answers HTTP 400 "requires an AAD user access token. App-only tokens are
  not supported." with the app token, and HTTP 400 "cannot be used in conjunction with header
  x-ms-query-source-authorization" when a user token is also sent. The docs' OData path
  `/indexes('name')/search.post.search` answered HTTP 405 (POST). So an elevated read needs a signed-in USER
  (the admin) with the Search Index Data Contributor role as the Authorization bearer, on the ordinary
  `/indexes/{name}/docs/search` path.
- Persona sign-in for `https://search.azure.com/.default` FAILS with AADSTS65001 ("user or administrator has
  not consented to use the application ... leaklab-persona"): the persona app has no consent for the Azure AI
  Search resource. Until it does, no persona query (and no admin elevated read through the same app) can run.
- Admin device-code sign-in for the same scope also fails (2026-10-17): AADSTS650057 "Invalid resource ...
  Resource value from request: https://search.azure.com. Resource app ID: 880da380-985e-4198-81b9-e05b1cc53158.
  List of valid resources from app registration: 00000003-0000-0000-c000-000000000000" (= Microsoft Graph only).
  So `https://search.azure.com` IS resource app 880da380-985e-4198-81b9-e05b1cc53158, and the persona app
  must have a delegated permission on it added in its API permissions (then admin consent) before any user
  token for the search service can be issued through it. Which scope name that resource offers: UNVERIFIED.

### Setup A probes, trimming and revocation (2026-10-17; probes/out/search_acl_trim_*, _revoke_*, _entra_add_lag_*)
Service `leaklab-search-772918cf`, plain SharePoint-indexer ACL configuration (client-secret ingestion app, no SharePoint-group
configuration), api-version 2026-08-01-preview, tenant generated from the 10-folder spec. All numbers below are single runs
unless a count is given.
- USER TOKEN: after adding the Azure AI Search resource (app 880da380-985e-4198-81b9-e05b1cc53158) to `leaklab-persona`'s API
  permissions with admin consent, persona tokens are issued with `aud` = `https://search.azure.com`, `scp` = `user_impersonation`
  (scope requested: `https://search.azure.com/.default`). The caller's own Authorization bearer is the DRIVER app's app-only token for
  the same scope; the user's token travels in `x-ms-query-source-authorization`. Without it: HTTP 200, 0 documents (protected
  documents are not returned; verified for a persona query and for an app-token-only query).
- BASIC TRIMMING (token as above, `search=*`, top 1000), compared with the oracle: p-intern 6/6, p-finana 12/12, p-finlead 16/16,
  p-hr 12/12, p-ceo 5/5 (exact match, 0 leaks, 0 lockouts); p-plateng 21 of 23 expected, 0 leaks. So the four UNLICENSED personas are
  trimmed correctly (a license is not needed for the search ACL view), and one canary queried by exact string came back only for the
  persona allowed to read it. Union over the six personas = 41 documents; the other 2 (S1-SPGroup) are returned to nobody.
- GRANT TYPES in the index (inferred from who gets what; the stored values themselves could not be read, see below):
  Entra group grant (S1-Entra): honored (p-plateng, a member through `grp-s1-entra`). Direct user grant (S1-Direct): honored.
  SHAREPOINT-GROUP grant (S1-SPGroup, p-plateng is a direct member of the SharePoint group): NOT honored in the plain
  configuration: p-plateng does not get the 2 documents. Whether the grant was stored as an unmatched value, as an `spg:` id, or
  dropped is UNVERIFIED: an elevated read (needs a signed-in user with Search Index Data Contributor; the admin has no data-plane
  role yet: HTTP 403) was not possible.
- `documentCount` in index statistics is a LAGGING number: 36 right after the first run (and for minutes), 43 later, although
  queries already returned 41 distinct documents.
- Indexer: first run (started by the indexer's creation) 18 s for 43 items, 0 failed.
- GROUP MEMBERSHIP IS EVALUATED AT QUERY TIME, with a delay of minutes in BOTH directions, and independently of the token's age:
  removing p-plateng from `grp-s1-entra` (no indexer run): the S1-Entra documents were still served to BOTH an old token (taken
  before) and a fresh one until 187 s (run 1; the probe died of Entra throttling after that) and until between 289 and 301 s
  (run 2, 12 s polls), then denied to both. Re-adding the member: not served at 3.9-119.6 s and at ~180 s in one check, served at
  176 s after the start of the next poll series (run of probes/out/search_acl_entra_add_lag_*.json), i.e. ~3 minutes. A first,
  earlier check about 3 minutes after the re-add had still found the arm not served. So a query-time membership cache of roughly
  3-5 minutes is the best reading; the exact figure is UNVERIFIED (2 removals, 2 additions).
- DIRECT GRANT REMOVAL: with the grant removed on the S1-Direct FOLDER (the 2 documents inherit it), the index kept serving p-plateng
  for the whole time until the indexer was run (21 polls, 253 s): the ACL is FROZEN in the index. An ordinary (incremental) indexer
  run 4 minutes after the change processed exactly 2 items in 3.4 s (wall 12 s) and the very next query denied p-plateng. So the
  incremental run DID pick up a folder-level (parent-scope) change for its children, because of the "folder propagation" that the
  service enabled by itself ("Folder propagation was automatically enabled because ACL ingestion is configured...
  propagateFolderChanges=true"): this contradicts the docs' statement that parent-scope changes need /resync. A later resync +
  run (`resync` HTTP 204, then a run of 43 items in 17.8 s) changed nothing visible (already denied).
  Consequence for Setup A: a direct/folder grant change is invisible until an indexer run (nothing schedules one here), then
  effective within seconds of the run's end; a group-membership change is effective after a query-time lag of ~3-5 minutes.
- Entra throttles repeated password sign-ins: a fresh persona token on every poll gave AADSTS50196 ("client request loop") after
  about 20 polls; the probe now takes a fresh token every 120 s.
- NOT YET DONE: elevated read / stored ACL values (needs the admin data-plane role), the SharePoint-group second phase
  (federated credential + SharePoint API permission + sharePointConnectorAppRegistration), a revocation of the SharePoint-group arm
  (nothing to revoke), whether the index ACL of S1-Direct is correct after the tenant was regenerated.

### Setup A: the ACL values the indexer stored (elevated read, 2026-10-17; probes/out/search_acl_dump_20261017T094854Z.json)
Elevated read worked once the endpoint was the one the admin holds Search Index Data Contributor on (HTTP 200, 43 documents):
admin user token as the Authorization bearer, `x-ms-enable-elevated-read: true`, ordinary `/indexes/{name}/docs/search` path,
no x-ms-query-source-authorization. (The earlier 403s came from pointing at the other service; see the endpoint note.)
What the plain configuration (no SharePoint-group setup) stored, per folder, in `UserIds` / `GroupIds`:
- Entra group grant -> the group's object id in GroupIds (Board, Engineering, Finance, Finance/Leadership, HR,
  Platform-Secrets, S1-Entra: one id each, nothing expanded to members).
- Direct user grant -> the user's object id in UserIds (Board: p-ceo; special file: p-ceo and p-hr).
- SHAREPOINT-GROUP grant (S1-SPGroup) -> STORED, not dropped: GroupIds = `spg:https://leaklabdev.sharepoint.com/sites/leaklab:168`
  (the prefix `spg:`, the site url in lower case, the SharePoint group id 168). Yet p-plateng, a member of that group, was not served
  those documents (trim stage): without `sharePointConnectorAppRegistration` the value is stored but never matched at query time.
- Inherited documents (General, via the site root) carry grp-all-staff AND `spg:...:3`, `spg:...:4`, `spg:...:5` (the site's own
  Owners, Members and Visitors groups, the associated groups seen earlier). Those three are therefore stored too and equally unmatched.
- The ADMIN's object id (1a993a79-..., identical to the admin token's `oid`) is in UserIds of EVERY document, including the unique
  folders (the admin is a site collection admin / Owner). The oracle excludes the admin by decision; the search index includes it.
- An empty list is stored as the one string `none`: GroupIds = ["none"] for S1-Direct (user grant only) and for the special file.
- The S1-Direct documents have NO p-plateng id in UserIds: the index is stale there. The grant was removed in the direct-arm test,
  the incremental run recorded the removal, and the tenant was regenerated afterwards (the grant is back in SharePoint) without a new
  indexer run. This is the frozen-ACL behavior seen from the inside.
- Consequence for the oracle/runner: for Setup A the admin must be treated as allowed everywhere (or excluded from comparison as now);
  `spg:` ids and "none" are values the Setup B overlay will meet if it reads the same index.

### Prompt 18 probes: lookup by key, and the smallest indexer schedule (2026-10-17)
(a) CAN A DOCUMENT BE FETCHED BY KEY WITH THE ADMIN KEY WHILE PERMISSION FILTERING IS ON?  NO.
(probes/out/search_acl_lookup_*.json; the admin key was read from the management plane by the driver app, which holds Search
Service Contributor, because the key in `.env` belonged to the other service: HTTP 403.)
- The key of a document is derivable: URL-safe base64 of `<site id>::<drive id>::<drive item id>` where the item part is Graph's
  driveItem id PLUS one extra character "5" (6 of 6 documents compared; after adding it, lookups by derived key succeeded).
  `src/leaklab/setup_a/keys.py`. Empirical, not documented.
- GET /indexes/{index}/docs/{key} answers: admin api-key only -> 404 (empty body) for a document that exists AND for one that does
  not; driver bearer only -> 404; driver bearer + a user token that may read it -> 200; + a user token that may not -> 404.
  So every lookup is ACL-trimmed, the admin key included (as the docs' November-2025 note says), and a 404 cannot tell "not indexed"
  from "not allowed".
- Consequence: setup_a_basic cannot use a lookup for its presence check. It uses evidence the filter cannot hide: (1) any persona
  was served the canary through the system, or (2) the indexer completed a successful run with 0 failed items, none naming the
  document, that STARTED after the document was created (Graph createdDateTime). Otherwise served=None, error "not_indexed"
  ("presence_unknown" if the creation time is unknown), never served=False. Limit: (2) shows the run saw the document, not that the
  index holds it. An elevated read would be the exact method but needs a signed-in user with Search Index Data Contributor (a
  dedicated test user with that role would allow it from code; not set up).
(b) THE SMALLEST INDEXER SCHEDULE: 5 minutes. Docs (search-howto-schedule-indexers): interval 5 minutes to 1,440 minutes (24 h),
`P(nD)(T(nH)(nM))`, the interval is between the START of two runs, an overlapping run is postponed to the next slot. Set on the
service: PT1M, PT2M and PT4M were all rejected (HTTP 400 "Indexing schedule interval 00:0nM is shorter than the shortest supported
interval 00:05:00"); PT5M was accepted (204) and reads back as {"interval": "PT5M", "startTime": ...}. The indexer is on PT5M.
`src/leaklab/setup_a/` (system "setup_a_basic"): client.py (SearchPort, HttpSearchPort), system.py (SetupABasic), provision.py
(idempotent data source/index/indexer with PT5M; no SharePoint-group configuration), keys.py. Persona tokens are reused 120 s.
Indexer runs are reported by the system (`drain_events`), journaled by the engine as `system_event` and shown in the report
(`indexer_runs_after_change`, `first_indexer_run_end_after_change_s`, column "idx run s").

### Setup 0 vs setup_a_basic, one run, same polling loop (2026-10-17; runs/run_20261017T102152Z.jsonl, git-ignored)
`--scenario S1-direct --scenario S1-spgroup --scenario S1-entra --systems setup0,setup_a_basic`, 5 s cycle, baseline 5 reads per
pair, n = 10, cap 60 min, one replicate, no errors in any of the 6,000+ reads. Setup A: indexer on PT5M (its runs ended every ~5 min).
Times are seconds after the mutation's `confirmed_at`; "idx run" = when the first indexer run ENDED after the change.

| scenario | system | first denial | leak exposure | converged | idx run | verdict |
|---|---|---|---|---|---|---|
| S1-direct | setup0 | 1.7 | 0.0 | 1.7 | - | OK |
| S1-direct | setup_a_basic | 143.6 | 138.2 | 143.6 | 141.4 | LEAK |
| S1-spgroup | setup0 | 4.8 | 0.0 | 4.8 | - | OK |
| S1-spgroup | setup_a_basic | (nothing to revoke) | - | - | 162.8 | LOCKOUT |
| S1-entra | setup0 | 2780.5 (46.3 min) | 3541.3 (59.0 min) | 3546.2 (59.1 min) | - | LEAK |
| S1-entra | setup_a_basic | 249.2 (4.2 min) | 244.5 | 249.2 | 108.1 | LEAK |

- S1-direct on Setup A: the document stayed served until the indexer run that ended 141.4 s after the change; the first denial
  came 2.2 s after that run ended. So the exposure is the wait for the next scheduled run (<= 5 min + run time), not a property of
  the query. (Setup 0: denied by the first read.)
- S1-entra on Setup A: the indexer run that ended 108 s after the change did NOT matter; the denial came at 249 s, the query-time
  membership lookup catching up (3 earlier samples: 187, ~295 s; now 249 s: the lag is 3-5 min, variable). Setup 0: first denial at
  46.3 min, 10 denials in a row only at 59.1 min (the previous runner run, 2 hours earlier, flapped until the 60-minute cap).
- S1-spgroup on Setup A: p-plateng was DENIED the S1-SPGroup documents the whole time (lockout 895 s = the baseline cap), so there
  was nothing to revoke: the SharePoint-group grant is not honored by the basic configuration. The runner flags it (BASELINE_UNSTABLE,
  `nothing_to_revoke` 2, convergence NOTHING_TO_REVOKE) and prints no revocation numbers for it. Setup 0 honored it (4.8 s).
- Report fixes made after this run (the journal is unchanged): the baseline is now judged PER SYSTEM from that system's own reads
  (the engine's baseline wait is joint, so Setup 0's S1-spgroup row had inherited "UNSTABLE" from Setup A; `baseline_joint` keeps
  the engine's value); a revoked pair that was not served before the change reports no first denial/exposure.
- Stack: 385+ tests; the new code was checked with 18 planted bugs, all caught (15 first time, 3 after strengthening a test and
  fixing two mutant patterns).

## Setup B: authoritative state (src/leaklab/setup_b/state/), Prompt 19a (2026-10-17)

### Probe: does Graph drive delta report permission changes? YES, but only with three Prefer headers
(probes/delta_probe.py, probes/out/delta_probe_20261017T123522Z.json; four header sets polled side by side every 5 s while
real changes were made and undone; docs: graph/api/driveitem-delta)
- A none, B `Prefer: hierarchicalsharing`: NOTHING reported for any permission change (0 items in 90 s, every change).
- C `Prefer: deltashowremovedasdeleted, deltatraversepermissiongaps, deltashowsharingchanges` and D (C plus hierarchicalsharing):
  the same, reported 4.7-8.7 s after the change (the 5 s poll included):
  | change | items returned | flagged `@microsoft.graph.sharedChanged: true` |
  | folder S1-Direct: grant removed / added | 3 (drive root, ll-lab, S1-Direct) | S1-Direct only |
  | file offer-letter-draft.txt: grant removed / added | 4 (root, ll-lab, General, the file) | the file only |
  | SharePoint group sp-s1-spgroup: member removed / added | 3 (root, ll-lab, S1-SPGroup) | S1-SPGroup only (the folder that grants to the group) |
  | Entra group grp-s1-entra: member removed / added | 0 | nothing: an Entra membership change is NOT reported |
  So the flag marks the SCOPE (the folder or file with its own role assignments); its children are not listed; the ancestors come
  along unflagged; a SharePoint-group membership change is reported on the scopes that grant to the group.
- Not probed: changes to the SITE ROOT's role assignments, breaking/restoring inheritance, deletes, moves.
  Decision: acl_store uses the drive delta (3 headers, sent on EVERY call) as its 5 s signal, re-reads the unique flag of every
  item the delta returns, re-reads the role assignments (REST, SharePoint groups expanded via REST) of flagged/new/newly-unique
  scopes only, and runs `verify_all()` (every scope + every unique flag, root included) every 60 s as the safety net; a change first
  seen by the verification is logged as found_by "verify". A pure polling fallback (`poll_scopes`: unique scopes + root every cycle) exists.

### Probe: Graph group delta (probes/group_delta_probe.py, probes/out/group_delta_probe_*.json)
- `GET /groups/delta?$select=displayName,members`: the tenant has 225 groups (system groups included); the first enumeration returns
  each with its FULL membership in `members@delta` ({"@odata.type": #microsoft.graph.user or group, "id"}).
- A removal arrives as the same entry plus `"@removed": {"reason": "deleted"}`; an addition without it.
- ONLY the changed group appears: a user leaving a child group does not put the parent in the feed (nesting is computed locally).
- `Prefer: return=minimal` on the delta link: only changed properties (no displayName), same latency.
- Appears 11-24 s after the call (user and group members alike; the same lag as transitiveMemberOf).

### What was built
membership_store (group delta poll, edges + transitive closure table + directory, last_success_at, event log), acl_store (drive delta
+ REST, scope tree, grants with SharePoint groups expanded, event log), StateReader (decide/can_read/readable; fail closed: stale or
never-succeeded pollers, unknown documents, unknown claims, unresolved users all DENY with a reason), PgRepository + MemoryRepository
behind one StateRepository protocol, `python -m leaklab.setup_b.state up|once|status`, docker-compose.yml (Postgres 16, 127.0.0.1:5433,
password only from LEAKLAB_PG_PASSWORD). Event log: every change has `seen_at` (the poll returned) and `applied_at` (just before the commit).
- Every poll is ONE atomic commit (state + events + last_success_at). A failed poll changes nothing, records the error, and does NOT move
  last_success_at. A dead link (HTTP 410) triggers a full re-enumeration whose events are only the differences.
- Grant kinds: `unknown` (special claims such as "Everyone", the site's own groups as seen, an Entra group inside a SharePoint group) never
  grant; "Limited Access" is dropped; only Read, Edit, Full Control grant read (as the oracle).
- Closure: recomputed in full at every change (cycle-safe). Fine for tens of groups; at enterprise scale it needs an incremental update.

### Bug found by the oracle property test (and fixed)
Remove p-hr from grp-hr: the store stopped letting p-hr read the file p-hr has a DIRECT grant on. Cause: the directory (object id <-> UPN) only
covered users who are members of some group; p-hr had left their only group, so the UPN of the user grant could no longer be matched to the
id. Fix: directory.py covers every user in an edge OR named by a user grant. Regression tests added; a 250-example x 10-mutation run passes.

### Verification
- Contract tests run on the in-memory AND a real PostgreSQL 16 (embedded `pgserver`, or LEAKLAB_TEST_PG_DSN): same results.
- Property test (Hypothesis, up to 6 mutations: grants on unique scopes for users/Entra groups/the SharePoint group, user and nested-group
  membership changes, SharePoint-group members, file moves; plus each spec scenario): after the sequence, what the store says each persona may
  read EQUALS the oracle's expected access, a further poll finds nothing, `verify_all()` finds no drift. The oracle is imported by tests only
  (tests/test_setup_b_isolation.py scans src/ for it; setup_b does not import the spec either).
- Planted bugs: 19 in the stores (no nesting, removals ignored, SharePoint groups not expanded, unknown/Limited Access granting, stale data
  trusted, a flagged scope not re-read, container filter off, a failed poll moving last_success_at, events not logged, unique flag not re-read,
  scope resolution wrong, closure not saved, memory repository without rollback, Postgres keeping old edges, deleted groups keeping edges):
  all 19 caught.
- LIVE (tests/test_setup_b_state_live.py, real Graph + SharePoint + embedded Postgres, 308 s): after the first sync the store equals the oracle
  for all six personas; three real changes, seconds from the mutation call to the reader's new answer (p-plateng loses the arm):
  direct grant removed 8.2 s (6.1 s after confirmed_at); SharePoint-group member removed 8.1 s (6.3 s); Entra-group member removed 9.6 s (4.0 s);
  `verify_all` found 0 drift events; after the restore the store equals the oracle again. The call-to-answer times include apply_mutation's own
  confirmation polling (several seconds) and the 5 s poll interval. Seen->applied per event 0-2.8 s (the REST reads inside a poll).
  ONE run; the earlier group-delta probe saw 11-24 s for Entra changes, so expect that spread.

### Limits and open questions
- Docker (verified by the user, 2026-10-17): `docker compose up -d` pulled postgres:16 and started it healthy on 127.0.0.1:5433;
  `python -m leaklab.setup_b.state once` then ran both stores against the real tenant on it (membership: full enumeration, 35 edges;
  acl: 68 events for the first sync). The first attempt failed only because LEAKLAB_PG_DSN pointed at port 5432 / user postgres instead
  of the compose database (5433, user and database `leaklab`); PgRepository now reports host/port/dbname/user (never the password) and
  times out after 10 s.
- Timings on the real tenant (compose Postgres): delta polls 0.4 s each when nothing changed (links persist across runs, so a restart
  does not re-enumerate); the first ACL sync about 38 s; `verify_all` 29.5 s (every unique flag and every scope: tens of REST calls).
- FIXED after measuring: in one sequential loop the 30 s verification held the membership poll back (membership age 38 s > the 30 s
  freshness limit: the reader would have DENIED everything for those seconds). `up` now runs membership, ACL delta and verification as
  three independent threads, and verify_all works in two phases (reads without the lock, then a merge under the lock that keeps whatever
  a delta poll wrote meanwhile: merge_verified). Measured with `up` running 140 s (two verifications inside) and a second process sampling
  the reader every 5 s: worst membership age 5.2 s, worst ACL age 5.2 s while running, 0 of 30 samples stale.
- Site-ROOT permission changes, inheritance break/restore, deletes and moves in the REAL drive delta were not probed; the fake delta used in the
  tests emulates only what the probe showed. verify_all (60 s) is what bounds those.
- Scale: the ACL snapshot is replaced whole at each change and the closure is recomputed whole; fine here, not for thousands of scopes.
- Nested Entra groups inside SharePoint groups are not expanded (unverified; also not by Azure AI Search): they deny.
- The reader does not talk to Graph or SharePoint: a user the directory could not resolve has no UPN, so user grants deny for them.

## Setup B: service API (src/leaklab/setup_b/api/), Prompt 19b

### Probe: Setup B's own index, and token validation (2026-10-17; probes/search_index_b_probe.py)
Why a second index: the service (app-only) token sees 0 documents in `ll-sp-index` (permissionFilterOption enabled; see the Setup A
probes above), so Setup B cannot post-filter anything there. Decision (user): a SECOND index over the SAME SharePoint data source,
identical to `ll-sp-index` except permissionFilterOption. Setup A is untouched.
- probes/out/search_acl_index_b_create_20261017T143226Z.json: `ll-sp-index-b` was created (HTTP 201) from the SAME body as `ll-sp-index`
  (same fields, same `permissionFilter` attributes on UserIds/GroupIds, same analyzers/defaults, same schema) with ONLY
  `permissionFilterOption: "disabled"`; the first variant was accepted, so there is NO difference other than that option.
- A second indexer `ll-sp-indexer-b` on the SAME data source `ll-sp-datasource`, same body and the same `PT5M` schedule, was accepted
  (HTTP 201). Its first run: 14:32:06.9 -> 14:32:20.9 (14 s), success, 43 items processed, 0 failed.
- Queried with the SERVICE token only (driver app, `https://search.azure.com/.default`, no x-ms-query-source-authorization):
  `search=*` returned all 43 documents (`@odata.count` 43), against 0 for `ll-sp-index`. The index statistics endpoint still said
  `documentCount: 0` right after the run (the same lagging counter as in `ll-sp-index`).
- Keys in index-b parse with `setup_a/keys.parse_key` for all 43 documents and the item part ends in "5" for all 43, as in
  `ll-sp-index`: the drive item id is the key's item part minus that last character (empirical, as already recorded).
- Each index has its OWN indexer, so the two indexes refresh on their own 5-minute cycles (the cycles are offset by when each indexer
  was created). CAVEAT: content freshness of the two is comparable, not simultaneous, and a new or changed document can appear in
  one index minutes before the other. The runner's "not_indexed" handling therefore has to be per system.
- The ACL fields of index-b (UserIds/GroupIds) hold the same snapshot the indexer stored for index-a. Setup B NEVER decides from them
  (they are stale snapshots); /explain may show them for comparison only.

### Probe: persona token validation (probes/out/search_acl_token_20261017T143117Z.json)
- A persona token for scope `https://search.azure.com/.default` has `aud` = `https://search.azure.com`, `scp` = `user_impersonation`,
  `iss` = `https://sts.windows.net/<tid>/`, `tid`, `oid`, `upn`, `appid` (the persona app), header `alg` RS256 and a `kid`.
- The tenant's keys at `https://login.microsoftonline.com/<tid>/discovery/v2.0/keys` returned 5 keys including this token's `kid`;
  PyJWT verified the signature (RS256), issuer and audience; the same token with another audience raised InvalidAudienceError.
- There is no API app registration (the driver app has no right to create one), so Setup B's API accepts tokens whose audience is a
  CONFIGURED value, by default `https://search.azure.com` (the tokens the runner already has). Consequence: any token issued for
  Azure AI Search to a user of this tenant is accepted by /query. Acceptable in the test tenant; a real deployment needs its own
  audience. UNVERIFIED: that v2.0-format tokens (iss `login.microsoftonline.com/<tid>/v2.0`) behave the same: both issuers are accepted.

### What was built (src/leaklab/setup_b/api/, setup_b/system.py; threat model in docs/ARCHITECTURE.md)
- POST /query {persona_token, query, k}: validate the token (RS256 signature from the tenant JWKS, issuer, tid, audience, exp, a USER
  token with oid/upn/scp), search `ll-sp-index-b` with the service identity for k*5 hits, decide EVERY hit with the state reader
  (acl_store x membership_store; the index ACL fields are never read), write all decisions + one summary record to the hash-chained
  log (if that fails the request fails), return the first k allowed hits (doc id, name, content).
- GET /health: ok | degraded (reasons, ages) | down. GET /explain?user=&doc=: the path that allows or denies (groups chain, matching
  grants, scope), plus the index's stale ACL snapshot "for comparison only". GET /metrics: poller lag, decision counters, recall numbers.
  /explain and /metrics need an operator key (disabled if none is configured). `debug` stats only if the server allows them.
- Decision log: Postgres table `leaklab_api.decisions`, sha256 chain (previous hash + canonical JSON), advisory-locked appends, `verify()`
  finds the first broken record; stores user oid, doc id, allowed, reason, request id, counts; never tokens, query text (sha256 only) or content.
- Degraded mode (state/access.py): membership stale -> group-based grants denied, grants naming the user still honored; ACL stale ->
  everything denied (STRICTER than the prompt, which only names group-based grants: a revoked direct grant would otherwise be honored
  from stale state); unknown principal -> denied (`unknown_principal`).
- Adapter `setup_b`: POST /query with the persona's token and the exact-phrase query; served = canary string in a returned document. It uses
  the debug stats' pre-filter count: an exact phrase matching nothing (fetched == 0) is `not_indexed` (served=None), never a denial; without
  debug stats the read is an error (`no_debug_stats`), never a denial. Journal events: `system_health` (status changes) and
  `postfilter_recall` (broad query "*" at k = 3, 5, 10 for every persona: fetched, allowed in the window, returned, readable among ALL
  matches, shortfall, lost vs an unlimited window), sampled at start and every 5 minutes. The report carries `system_labels`
  ("Setup B queries a separate, unfiltered index over the same SharePoint data source; Setup A uses the ACL-filtered index."),
  `postfilter_recall` and `system_health`.
- The runner builds Setup B with a FRESH state schema per run (first sync ~43 s), the pollers and the API in the same process
  (127.0.0.1, random port); Setup A and Setup B share one persona-token cache (Entra throttles repeated password sign-ins).

### Verification (Prompt 19b)
- 575 tests pass (4 live skipped by default): token validation (wrong audience/tenant/issuer, expired, foreign key, unknown kid, `none` and
  HS256 downgrade, app-only token, missing identity), the hash chain on memory and Postgres (changed, deleted, reordered, re-hashed
  records; 30 concurrent appends make one chain), /query for all six personas EQUALS the oracle's expected access, a revocation is followed
  after the next poll, stale membership / stale ACL / nothing polled / unknown principal / index down / log down, the log holds no token,
  query text or content, /explain shows the state's decision even when the index's stale ACL says the opposite (both directions),
  operator key, bounded metric labels, the HTTP clients with mock transports (service bearer only, no user header, key mapping,
  every bad answer -> IndexUnavailable, JWKS cache and throttled refetch), the adapter against the real app.
- 8 planted bugs in the API layer, each caught: results ignoring the decision, k not applied, stale ACL not denying, stale membership
  still honoring groups, audience not checked, tenant not checked, log failure ignored, operator key not compared.
- LIVE smoke (real tenant, real index-b, real Postgres, personas' real tokens, before the S1 run): `build_setup_b` 43 s, 258 reads
  (6 personas x 43 canaries) through the HTTP API: 0 errors, 0 mismatches against the oracle.

### S1 arms, three systems together (2026-10-17; runs/run_20261017T150835Z.jsonl, ONE replicate, interval 5 s, cap 60 min)
Setup B queries a separate, unfiltered index over the same SharePoint data source; Setup A uses the ACL-filtered index.
Times in seconds after the change was confirmed; "leak" = how long after it the system still served the revoked canary.
| arm | system | baseline | first deny | leak | note |
|---|---|---|---|---|---|
| S1-direct | setup0 | OK | 2.2 | 0.0 | (1 read error) |
| S1-direct | setup_a_basic | OK | 238.9 | 198.6 | frozen ACL until an indexer run ended (226.5 s after the change) |
| S1-direct | setup_b | OK | 7.3 | 2.3 | |
| S1-spgroup | setup0 | OK | 5.2 | 0.0 | |
| S1-spgroup | setup_a_basic | UNSTABLE | - | - | the group grant is stored but never honored: nothing was ever served, lockout 865 s (nothing to revoke) |
| S1-spgroup | setup_b | OK | 8.7 | 5.0 | |
| S1-entra | setup0 | OK | 2700.5 | 3560.5 | did not converge in the 60 min cap (the known 36-59 min flapping) |
| S1-entra | setup_a_basic | OK | 273.9 | 270.0 | query-time membership lag |
| S1-entra | setup_b | OK | 7.5 | 0.0 | |
- Setup B was never denied for staleness: all 9,887 logged decisions have reason `allowed` (2,936) or `no_matching_grant` (6,951); `/health`
  reported `ok` for the whole run (one reading, no status change). No Setup B read was an error.
- Setup B's leak windows (2.3 s and 5.0 s) are the poll interval plus the drive-delta lag measured before (4.7-8.7 s), not a defect of the
  filter; the S1-entra arm shows 0 leak because the Graph group delta (11-24 s) had already reported the change by the time the mutation was
  confirmed. ONE replicate: these are single observations, not distributions.
- The decision log written during the run (13,054 records, including the smoke test and earlier runs of this schema) verifies: chain ok.
- RECALL COST of post-filtering (broad query "*", window k*5, 234 samples = 6 personas x 3 k x 13 rounds): returned vs the best
  possible (readable documents among ALL matches, capped at k):
  k=3: 195 returned of 234 possible (26 of 78 samples short of k, 39 results lost); k=5: 364 of 390 (26 short, 26 lost);
  k=10: 663 of 663 (26 short because the persona cannot read k documents at all, 0 lost). So the over-fetch window k*5 loses results for
  small k when forbidden documents rank first; "short of k" alone also counts personas with fewer than k readable documents.
  CAVEAT: the corpus is 43 documents and `*` gives an arbitrary but fixed ranking, so these numbers show the mechanism, not a production figure.
- Unavoidable differences between Setup A and B queries: index-b is a second index with its own indexer cycle (freshness comparable, not
  simultaneous); Setup B pays one extra hop (HTTP + state reads) and the recall cost above.

## Open questions

- [ ] Persona sign-in works for two LICENSED users. Not yet tested: UNLICENSED personas (four in
  the spec: p-finana, p-finlead, p-hr, p-ceo), whether any "not propagated yet" error exists
  (none seen), and the MFA/consent codes (`INTERACTION_ERROR_CODES` in `auth.py`).
- [ ] Whether the license matters for SharePoint access or for the Azure AI Search ACL view: both
  personas in the access test were licensed (license lag: 0.6s and 12.7s, see above).
- [ ] The retry policy (`http_retry.py`: 429/503/504, Retry-After, backoff) is only unit-tested
  with mocks; no real throttling response has been seen yet.
- [ ] How long can transitiveMemberOf stay inconsistent, and does it flap? Two samples: 11.6s with a
  2 -> 0 -> 3 sequence, and 12.3s with a clean 0 -> 3. Both are about 12s; the tail is unknown.
  Also check how the SharePoint/Azure AI Search side sees the same change.
- [ ] Adding a user/group that is already a member: the assumed error (HTTP 400, "already
  exist") is now verified (see Idempotency). Still unverified: re-creating a deleted user's UPN
  immediately (only a gap of minutes was tested; deletes lag behind lookups).
- [ ] WHY an Entra-group grant sometimes gives no access at all (5 of 11 runs), sometimes immediate
  access (4), sometimes delayed or flapping access (2), with an identical recipe. Not the user's age, not
  the names, not the token. Untested: the order of calls (the SharePoint principal for the group and
  the grant exist BEFORE the user is added to the Entra group, in every run); the time of day; the time
  between group creation and the add. A run that adds the user to the group FIRST and creates the
  SharePoint principal and the grant afterwards would test the order.
- [ ] RE-CREATED NAMES: no longer a good candidate. Brand-new names (`a1`) gave a delayed first access
  and coin-flip reads; the same names re-created (`n1` twice) gave 452s and flapping; the first use of
  `n1` and of the original names was clean. What separates the clean runs from the others is
  unknown (the user's history, the time of day, the state of the service, or chance).
- [ ] ENTRA-GROUP GRANTS vs. THE OTHER TWO (top priority). Two three-arm runs agree: DIRECT and
  SHAREPOINT-GROUP grants end within about 16s; an ENTRA-group grant keeps allowing reads for about
  36 minutes (first denial 35.5-36.3 min, both tokens, both runs), then the reads flip (27-44%
  denied) and had not settled after 45 minutes, while SharePoint's own effective permission still said
  "can read". What determines the 36 minutes? A first try with `--phase1 120` was inconclusive: the Entra arm never
  had access in that run. Experiments, each needing a new user or new names (the probe now
  waits for a baseline and flags arms that never had access):
  (1) vary phase 1 (`--phase1 120` vs `--phase1 1500`) on runs where the Entra arm DID get access: does the
  first denial stay at about 36 minutes after the REMOVAL, or move with the time since the member
  was ADDED or first read? (2) sparse
  polling (a few reads only around 30-45 minutes) to see whether constant reading holds the access
  open; (3) a longer phase 2 (90 minutes) to see whether, and when, denials become consistent;
  (4) a fourth arm: a SharePoint group that CONTAINS the Entra group (the common enterprise
  pattern); (5) a nested Entra group (S2); (6) removing the Entra group's role assignment on the
  folder instead of the membership (S1).
- [ ] REVOCATION TIMING (the core question for S1/S2/S6). The clean result below (new names) must be
  repeated with new names each time and `--baseline-reads 3`. Direct membership removal: SharePoint kept
  allowing reads, and its own effective-permission answer stayed "Read", for 30 minutes with old and
  fresh tokens, although Entra dropped the membership within 37s (two setups, see above). Still open:
  (1) when, or whether, it ever turns 403 (a long, sparse poll, hours); (2) what makes SharePoint
  refresh its view (a new sign-in, an access through another path, a change to the grant itself);
  (3) nested removal (`--mode nested`, S2); (4) removing the folder's role assignment instead of the
  membership (S1), which does not depend on group membership at all; (5) removal of the user from
  a group nested elsewhere vs. direct, with new names each time.
- [ ] Why the admin keeps access to a unique-permission folder: site collection admin, or any
  member of LeakLab Owners (whose role assignment was removed from the folder)? Test a non-admin
  member of LeakLab Owners, then fix the oracle rule for the admin account.
- [ ] Graph breaking inheritance: not tried (REST works, so low priority). Children with their
  own unique permissions under `clearSubscopes=true`: not tested.
- [ ] Teardown of orphans: after Entra groups/users are deleted, their SharePoint principals and
  "Limited Access" assignments remain on the site root. How to remove them (e.g. site user removal)
  is untested.
- [ ] A real READ after a file move: the listing says the file keeps its own grants, but nobody has
  tried to read it as the granted user, as a member of the destination folder's group, and as an
  outsider.
- [ ] How long a brand-new Entra group takes to be resolvable by SharePoint `ensureuser` (variable;
  see above). The re-run of the two failed subcommands will record seconds.
- [ ] Special claims such as "Everyone except external users" and org-wide sharing links: not
  observed anywhere yet; the site has none. Needs a probe that grants one and reads it back.
- [ ] How long Graph and REST permission reads lag after a change (one Graph sample was
  immediate), and whether the Azure AI Search ACL view lags longer.
- [ ] What happens to a file's unique permissions when it is moved to another folder.
- [ ] Whether unlicensed users can be granted access (SharePoint read as an unlicensed persona has
  not been tested) and be trimmed correctly in Azure AI Search.
- [ ] Which token audience/scope the Azure AI Search ACL query requires for the end user.

## Benchmark harness (src/leaklab/benchmark/), built 2026-10-17, NOT YET RUN LIVE
- Spec change: S2 now has its own persona `p-nest`, groups `grp-s2-outer` > `grp-s2-inner` and folder `S2-Nested` (2 documents, code S2N),
  so it shares no user, group, folder, document or control pair with the S1 arms. `S2-nested-group-removal` removes grp-s2-inner from
  grp-s2-outer (control: General). The earlier scenario (grp-eng-platform out of grp-engineering) is kept as `S2-engineering-nested-removal`.
  Verified by test: the planner puts S1-direct, S1-spgroup, S1-entra, S2 and S4 into ONE polling loop (their watched pairs are disjoint).
  The tenant must be torn down and generated again to get the new objects (every benchmark run does that first). Canaries of the existing
  folders did not change. UNVERIFIED: that p-nest can sign in for the search scope, and the real lag of an Entra nested-group removal.
- Matrix: 5 runs; S1-direct and S1-spgroup in runs 1-3, S1-entra and S2 in runs 1-5, S4 in runs 1-2; baseline 1 min in odd runs and 10 min
  in even runs (k = minutes*60 / interval conforming reads); read interval 5 s, N=10, cap 60 min, 3 systems in one loop. Upper bound for
  the polling phases of the whole benchmark: 6.6 h (plus teardown/generate/settle per run).
- Setup B's journal events now include `poller_lag` (every 30 s) and the runs of the indexer behind ll-sp-index-b.
- Tests (fake tenant, fake clock, real engine journals): plan, resumable executor (crash -> resume from the journal, --redo), per-cell
  statistics, caveats generated from the data, HTML/JSON report, and a guard that the HTML template contains no numbers of its own.

## Benchmark, first full run (2026-10-18; runs/benchmark/report.html, report.json; 5 runs, journals run_20261018T*_r1..r5.jsonl)
Medians over the replicates (min-max in report.json); times in seconds after the change was confirmed; read interval 5 s; ONE tenant.
| scenario | setup0 first deny | setup_a_basic first deny | setup_b first deny |
|---|---|---|---|
| S1-direct (n=3) | 5 | 227 | 17 |
| S1-spgroup (n=3) | 6 | never served (3 of 3 had nothing to revoke) | 11 |
| S1-entra (n=5) | 2565 (1 did not converge in 60 min) | 164 | 10 |
| S2 nested removal (n=5) | 2663 | 234 | 9 |
| S4 move (n=2) | no change expected, none seen | no change, none seen | no change in access (but see lockouts below) |
Setup B's largest leak exposure after a change was 13 s (S2); none of its replicates failed to converge.
- PROBLEM 1 (Setup B start-up lockouts): setup_b withheld pairs it should serve at the START of runs 1-3: pairs were first served 6-12 s
  (run 1), about 90-98 s (run 2) and 25-31 s (run 3) after the run began; runs 4 and 5 had none. Direct user grants (p-ceo and p-hr on the
  special file, p-plateng on S1-Direct) were affected as well as group grants, and `/health` never reported degraded, so the state was
  fresh but incomplete. These reads count as `lockout` in the table (also S4 shows setup_b LOCKOUT 2 of 2: only these start-up reads).
  They happen BEFORE the change, so they are not revocation numbers, but they are real: a freshly generated tenant is not served
  correctly for up to ~100 s. Cause UNVERIFIED: the first sync right after `generate` may see SharePoint/Graph before the new
  permissions and memberships are visible, and a later poll or the 60 s verification completes it (run 2's ~90 s fits a verification
  cycle). To check: log the first sync's grant count against a later one, or wait a settle time after generate before building Setup B.
- PROBLEM 2 (stale copies in the index): `ll-sp-index-b` holds 268 documents for 45 live ones; every canary exists in up to 6 copies (one
  per tenant generation). Neither indexer removes documents of a deleted tenant. Setup B denies the stale copies (unknown item id) so
  nothing leaks, but they fill the k*5 window: the recall figures in this report (e.g. k=10: 1340 returned of 2436 possible) are
  CONTAMINATED and must not be quoted. `ll-sp-index` (Setup A) very probably has the same stale copies (not counted yet); their stored
  ACLs name principals of deleted generations, which is why they were not served, but that is not verified. Fix proposed, not done:
  delete the documents of earlier generations from both indexes (or reset them) after each teardown, then re-measure recall.
- Read errors (served=None, mostly right after each regeneration while the indexers catch up) are excluded from every number; setup_a_basic
  had the most (37-127 per cell).

## Fixes after the first benchmark (2026-10-18; docs/BENCHMARK_HANDOFF.md)

### 1. Per-generation canaries
- Every canary now carries the id of the tenant generation: `CANARY-G<6>-<folder code>-<nn>-<hex6>` and `CANARY-G<6>-SPC-<hex6>`.
  The generation is also part of the hash, so two generations never share a canary (tested: 5 generations, 45 canaries each, pairwise disjoint;
  a pattern without `{generation}` is refused by the spec validator).
- The id is derived from the TENANT: "G" + the creation time (epoch seconds, base 36, 6 digits) of the `ll-lab` container folder. VERIFIED
  2026-10-18: Graph returns `createdDateTime` for the folder (`2026-10-18T08:38:29Z` for the run-5 generation, `GTN3HC5`). Teardown deletes the
  container, so the next generate gets a new id. Any process finds the live generation by looking the container up (`Env.discover_generation`,
  called by `open_env`); `generate()` adopts it when it creates the container. The spec file only holds a template generation (`G000000`,
  offline use and tests). Limit: two generations created in the same second would collide (not possible with a 60 s settle after teardown).
- The oracle needed no change: it takes the spec, and `spec.with_generation(g)` carries the generation. The runner uses `env.spec` (the live
  tenant's), the journal's `run_started` names the generation, `build_report` scores with it, and `--resume` refuses a journal of another generation.
- Consequence: journals written before this change (runs/benchmark/, the first full benchmark) have canaries without a generation and cannot
  be re-scored with the new code; their `report.json` / `report.html` stay as they were. New sets use new folders (runs/benchmark_v2/).

### 2. Presence checks match the current item id
- Setup A: the query now also returns each hit's index key (`id`), which encodes the Graph item id (setup_a/keys.py). With the current item id
  of every canary (from the generator's tenant) a hit counts only if its item id IS that id. A stale copy with the same text is never served,
  never counts as evidence that the document is indexed, and is reported once as a `stale_copy_seen` event. Setup B's adapter does the same with
  the item ids the API lists in its debug stats (`doc_ids`) and the `doc_id` of each result. Tests: a stale copy with the same text (not served,
  not "indexed", reported once, a stale copy visible to a persona who may not read the current document is a denial), a hit without a usable key.

### 3. Index cleanup
- PROBE (probes/index_cleanup_probe.py, probes/out/index_cleanup_*_20261018T11*.json), before: `ll-sp-index-b` listed 268 documents with the
  service token: 45 of the current generation, 223 stale, 0 foreign keys. `ll-sp-index` (ACL-filtered): search count 0 for the service token,
  `documentCount` statistic 268 (so about 223 stale; the statistic lags, the stored documents themselves cannot be listed without an elevated
  read by an admin user).
- Delete by key works while permission filtering is ON: `POST /indexes/{index}/docs/index` with `@search.action: delete` and the key is accepted
  by both indexes (HTTP 200, status true per key, driver app with Search Index Data Contributor). One stale key: ll-sp-index-b 268 -> 267 within
  3 s (and gone from the listing); ll-sp-index `documentCount` 268 -> 267 within 15 s.
- Full cleanup: 222 stale keys deleted from each index, 0 failed. After about 20 s: `ll-sp-index-b` lists exactly the 45 current
  documents (its `documentCount` statistic still said 267: it lags much longer than the search count), `ll-sp-index` `documentCount` 45. The
  report right after the delete call still showed the old counts (the service applies deletes a moment later), so the cleanup now polls the
  counts for up to 45 s.
- How it runs now (setup_a/cleanup.py, `run_once`): after the tenant is ready, the generation's item ids go into `runs/generations.json` and every
  document whose key is not of the current generation is deleted from both indexes before the systems are built. ll-sp-index-b is enumerated;
  ll-sp-index cannot be (the service token sees nothing), so its stale keys come from the registry of earlier generations plus the stale keys found
  in ll-sp-index-b. Documents of generations from before the registry that are in ll-sp-index only (none found: the two statistics matched) would need
  an admin's elevated listing (`elevated_keys`). The before/after counts are journalled (`index_cleanup`) and shown in the report. A failure is
  recorded and does not stop the run.

### 4. Setup B readiness (`GET /ready`)
- `/ready` is 200 only when BOTH pollers have completed two polls in a row without changes and one full ACL verification has completed
  (a verification that found drift restarts the ACL streak; a failed poll resets its poller's streak). It is advisory: it does not change what
  /query answers (the fail-closed rules still decide). The runner waits for it in `prepare()` (cap 5 minutes), before any baseline read, and
  journals `setup_b_ready` (ready, `time_to_ready_s` from the start of the build, the readiness state) and `setup_b_startup`. Reads, and so lockouts,
  start after that point; if the cap passes, the run goes on and the report says the run was not ready.
- Start-up hypothesis test: the builder records the first sync's numbers (membership edges, directory users, ACL items/files/unique scopes/grants)
  and `setup_b_startup` compares them with the state at readiness, with the events the pollers found in between and how many the verification
  found. RESULT: see the next section (the verification run).

### 5. Verification run on clean indexes (2026-10-18; runs/benchmark_v2/, run 1 of the set, n=1 per scenario, generation GTN3PY6)
One replicate of every scenario on a freshly generated tenant, all three systems, 5 s reads. A single run: these are observations, not
distributions. Times in seconds after the change was confirmed.
| scenario | setup0 first deny | setup_a_basic first deny | setup_b first deny (leak) |
|---|---|---|---|
| S1-direct | 18 | 298 | 18 (0) |
| S1-spgroup | 6 | never served (baseline unstable, as before) | 10 (6) |
| S1-entra | 2743 | 93 | 10 (0) |
| S2 nested removal | 2954 | 207 | 16 (9) |
| S4 move | no change | no change | no change |
- NO Setup B LOCKOUT in any scenario (the first benchmark had them in every cell). Setup B had 0 read errors; Setup A and Setup 0 had 0 too. The 13
  readable pairs were first served by Setup B within 10 s of the first read, like Setup 0 (the first benchmark: up to 98 s).
- Readiness: ready 76.6 s after the build started (first sync about 43 s, then two quiet polls per poller and the first verification); `prepare()` found
  it already ready (waited 0.007 s). The first sync was COMPLETE: the state at readiness had exactly the same numbers (26 membership edges, 15 directory
  users, 57 items / 45 files / 11 unique scopes / 15 read grants), no event was found after the first sync, and the verification found 0 drift.
  So the hypothesis "the first sync after generate is incomplete and a later poll or the verification completes it" is NOT supported by this run. What
  is known: with the readiness gate (and about 45 s more between generate and the first sync, because the index cleanup now runs first) there was no
  start-up lockout. Whether the earlier lockouts came from reading before convergence, from the shorter gap after generate (Graph/SharePoint
  eventual consistency), or from something else is still OPEN; it needs a run with the gate on and the cleanup delay removed to separate the two.
- Index cleanup before the run (journalled): 45 stale documents (the previous generation) found and deleted from each index, 0 failed; ll-sp-index-b
  listed 45 before and 0 after (the new generation not indexed yet); ll-sp-index's statistic still showed 45 right after (it lags). No stale copy was
  returned by Setup A's index (`stale_copy_seen` events: none), and Setup A's presence checks used the current item ids with 0 errors.
- Recall from a clean index (broad query "*", window k*5, 91 samples per k): k=3: 169 returned of 236 possible (67 lost, 39 samples short of k);
  k=5: 325 of 392 (67 lost); k=10: 665 of 665 (0 lost; the 50-document window covers the 45-document corpus). The lost results are the genuine
  cost of post-filtering with a fixed window when forbidden documents rank first (the first benchmark's numbers were inflated by stale copies).
  "Short of k" also counts personas with fewer than k readable documents.
- Poller lag in the run: membership and ACL state ages at most 5.1 s and 5.3 s; 0 degraded readings.
- Overnight: 6 more runs of S1-entra and S2 are planned in this set (runs 2-7) and scheduled on 2026-10-19, 21 and 23 at 22:00 (docs/OVERNIGHT.md).

## Setup B recall: adaptive paging (Prompt 22, Part 0; 2026-10-18)
- The fixed k*5 over-fetch is replaced by paging: pages of max(k, 10) hits (`MIN_PAGE`) are fetched with `skip` and decided one after another until
  k are allowed, the index is exhausted (a short page) or `max_pages` (10) have been fetched. `QueryStats` and the debug stats now carry `page_size`,
  `pages`, `exhausted`, `capped`; the decision log's query record carries them too; the adapter's `postfilter_recall` events and the benchmark report
  show pages (mean / max) and capped queries. Only decided hits are logged. The old behaviour is the same code with `page_size=5k, max_pages=1`.
  UNVERIFIED: `skip` paging over `search=*` relies on the service returning ties in a stable order between calls (observed stable: no document
  was fetched twice in the live measurement, but this is not documented behaviour); the unit tests use a deterministic fake.
- LIVE re-measurement on the clean index (src/leaklab/setup_b/recall.py, probes/out/recall_20261018T135534Z.json; 7 personas, broad query "*",
  generation GTN3PY6, both services over the same state): returned vs the most the persona could have been given (readable among ALL matches, capped at k)
  | mode | k=3 | k=5 | k=10 |
  |---|---|---|---|
  | adaptive paging | 18 of 18 (0 lost; pages mean 2.4, max 5) | 30 of 30 (0 lost; mean 3.0, max 5) | 51 of 51 (0 lost; mean 3.9, max 5) |
  | fixed window 5k | 13 of 18 (5 lost, 2 of 7 queries short) | 25 of 30 (5 lost, 2 of 7 short) | 51 of 51 (0 lost) |
  No adaptive query reached the page cap. Cost: more index round trips (about 2-4 pages on average) for personas who can read little. One run, one tenant
  generation, 45 documents.

## S6: agent cache and memory (Prompt 22, Parts 1-2)
### What was built (src/leaklab/agent/)
- LangGraph graph (graph.py): embed -> cache lookup -> [guard] -> deliver cached | memory recall -> [guard] -> retrieve -> build contexts -> [context guard]
  -> generate -> write back -> record. NAIVE: one cache and one memory shared by every user, no permission metadata (sources and owner are None).
  GUARDED: every cache entry and memory item stores the source drive item ids and the tenant generation; on every hit, for every recalled memory item and
  again for every piece of context before the LLM call, `guard.check_sources` asks Setup B's STATE (StateReader, never the index ACL fields) whether
  the CURRENT user may read EVERY source. Deny when the sources are missing or empty, of another generation, any one is not allowed, the state is
  stale or unknown, or the check raises. A user may use another user's entry iff they may read all of its sources today.
- LLM: `StubLLM` quotes its context verbatim (the worst-case leaker); `RealLLM` (Anthropic, lazy import) exists for `explore --real-llm` only; `s6 run
  --real-llm` and a real LLM for a non-exploratory run are refused (tested). UNVERIFIED: RealLLM was never run against the API (no key used).
- Semantic cache and long-term memory in Postgres + pgvector (stores.py; cosine distance); the same contract tests run on memory and Postgres. The embedded
  server of `pgserver` ships pgvector 0.6.2 (verified); the docker-compose image does not, so S6 uses the embedded server by default (`--agent-db` takes a DSN).
  Embedding: `HashEmbedder` (hashing trick over content words, 256 dims, deterministic), pluggable. Thresholds: cache 0.65, memory 0.30. Every lookup is
  journalled with hit/miss and similarity. Memory items are the title / topic / reference-code lines of each retrieved document.
- Checkpointer: LangGraph `PostgresSaver` (verified in a custom schema 2026-10-18). It lets ANY caller read ANY thread id, so ownership is kept by us
  (threads.py) and enforced in GUARDED mode: another user's thread is denied for reading and writing; NAIVE shares threads.
- Retrieval adapters: Setup A (persona token in x-ms-query-source-authorization, index key mapped to the item id) and Setup B (/query).
### The S6 driver (agent/s6/), separate from the mutation runner; reuses its Journal, `apply_mutation` (S6b) and the oracle (scoring)
- S6a cross-user cache (p-finlead asks, p-intern asks a paraphrase); S6b cache after revocation (p-plateng loses the S1-Direct grant through the
  S1-direct scenario's mutation, 30 s settle, asks a paraphrase; the grant is restored); S6c shared memory (p-finlead asks, p-intern asks a DIFFERENT
  question that only the memory answers); positive control (p-intern asks, p-finana asks a paraphrase, both may read the General source: the hit
  must be served). n=3 per variant per arm; trial i uses follow-up i of the fixed, versioned query set (agent/queries.yaml, validated by
  `queries.validate`: paraphrases score 0.83 against their seed, above the 0.65 threshold; the memory questions score 0.51-0.57 against the seed
  (a cache miss) and 0.34-0.38 against the remembered fact (a memory hit)). The questions are written into the journal.
- Arms: agent_naive_a (retrieval A), agent_naive_b (retrieval B), agent_guarded_b (retrieval B + guard). setup0: not applicable (plain SharePoint has no
  index, cache or agent), stated in every S6 report.
- Scoring (scoring.py, from the journal alone): LEAK = a canary was delivered to a persona whose expected access at the end of that step (oracle
  IntentTimeline over the confirmed mutations) lacks it. INVALID = the designed situation did not occur (no cache hit / no memory hit for the critical
  question, a failed step, an unconfirmed revocation): never counted as OK; applied to the guarded arm as well, so "no leak" is only evidence when the
  guard faced a candidate (stricter than the prompt, which names naive arms). EXPOSURE SURFACE (stored canaries some persona may not read) is reported
  separately from leaks. Before every trial the driver waits until the arm's own retrieval returns the owner's source (a new tenant takes minutes to index).
- Tests (fake retrieval + fake state / a fake tenant whose access is the oracle's intent): both modes, both stores, every guard rule, the full driver and
  report; planted bugs, each caught: guard skipped, one source unchecked (only the first), shared thread readable (claim and history), INVALID counted as OK,
  memory guard skipped, context guard skipped, generation not checked, delivery time ignored. A finding from the tests: with ONLY the cache switched off a
  naive agent still leaks the same text through the shared memory.

### S6 live results (2026-10-18; runs/benchmark_v2/s6_20261018T150648Z.jsonl, generation GTN3ZDD, query set v2, n=3, stub LLM, hash embedding)
One fresh tenant generation, all three arms, every trial valid (no step error, no wait timed out). A LEAK is a canary delivered to a persona whose expected
access at that moment (oracle) lacks it. Verdict counts of the 3 trials per cell:
| variant | agent_naive_a (Setup A retrieval) | agent_naive_b (Setup B retrieval) | agent_guarded_b |
|---|---|---|---|
| S6a cross-user cache (p-finlead -> p-intern) | LEAK 3 | LEAK 3 | OK 3 (9 guard denials) |
| S6b cache after revocation (p-plateng) | LEAK 3 | LEAK 3 | OK 3 (6 guard denials) |
| S6c shared memory (p-finlead -> p-intern) | LEAK 3 | LEAK 3 | OK 3 (3 guard denials) |
| control (both may read the source) | SERVED 3 | SERVED 3 | SERVED 3 (no over-blocking) |
- The leaked canaries are exactly the ones expected: p-intern received Finance/Leadership (FLD) canaries and, because the seed answer also quoted Finance
  documents, Finance (FIN) ones (S6a); p-plateng received an S1-Direct (S1D) canary after losing that grant (S6b); p-intern received an FLD canary through the
  memory (S6c). Cache similarity of the critical question: 0.833 (paraphrases, hit) and 0.507-0.567 (S6c: a cache miss, a memory hit).
- The guarded arm faced a cache or memory CANDIDATE in every attack trial (that is what its guard denials are) and delivered nothing it should not.
  It also stores such answers: its exposure surface (stored canaries some persona may not read: 18 in S6a, 12 in S6b, 14 in S6c) is as large as the naive arms'
  or larger. The guard does not reduce what is stored, it controls what is delivered; the exposure surface is reported separately and is not a leak count.
- With only the cache off, a naive agent still leaks through the shared memory (tested; the paraphrase also matches the remembered fact).
- Setup A (naive_a, S6b): after the revoke-and-restore of one trial, Setup A's retrieval of the source took up to 272 s to come back for the same user
  (the longest wait of the run; consistent with its 5-minute indexer cycle, NOT confirmed against the indexer events). Setup B's waits were at most 4 s.
- VALIDITY FINDING from the first live run (s6_20261018T143915Z.jsonl, generation GTN3Y3F, query set v1): agent_naive_a S6b scored OK 3 although it was
  a naive arm: Setup A had not indexed the S1-Direct documents yet, so its retrieval answered with S1-Entra documents p-plateng may still read; the
  baseline wait only waited for ANY result. The cached answer held nothing that could leak, so "no leak" proved nothing. Fixes: (1) the query set (v2)
  names a `source` document per variant and the driver waits until the arm's own retrieval returns it; (2) scoring: a hit only counts if the seed answer
  holds a canary the asker may not (for S6b: no longer) read, else INVALID `nothing_sensitive_cached` (control: `nothing_readable_cached`). Re-scored
  with the new rule the first journal gives INVALID 3 for exactly that cell and nothing else changes. The rule is tested on that situation.
- Caveats (generated into the report): stub LLM that quotes verbatim (worst-case leaker, results measure the cache, memory, retrieval and guard);
  deterministic hash embedding with cache threshold 0.65 and memory threshold 0.30 (other embeddings change how often a naive agent leaks); one tenant,
  n=3 trials per cell that are not independent draws; setup0 not applicable; Setup A basic retrieval is frozen until its indexer runs; the guard relies on
  Setup B's state, which lags the tenant by seconds (S6b waits 30 s after the confirmed revocation); Setup B queries a separate, unfiltered index.
- Not verified: the real-LLM `explore` command (no API key was used); `skip` paging stability; behaviour of a different embedding.


## Replay bundles (Prompt 23a; 2026-10-19; src/leaklab/replay/, schema docs/schemas/replay_bundle.schema.json v1.0)

Exporter: `uv run python -m leaklab.replay export --runs runs/benchmark_v2 --out <dir>`; figures: `... figures --bundles <dir> --out runs/figures`. Real export of runs/benchmark_v2: 4 bundles, summary recompute (oracle `classify` over the bundle's own reads) equals report.json for every scenario x system; schema and redaction scans pass.

**Maps directly from the journal:** run id, generation, config (interval, cap, n_consecutive), reads (`obs`: time, system, persona, canary, served/error, status), mutation request/confirm times, indexer runs (`system_event` indexer_run), poller lag (`poller_lag`), setup_b ready time.
**Derived:** `expected` and `verdict` of every read (oracle IntentTimeline over the run's applied mutations; error / not_indexed -> "unscored"), `t_rel_s`, document labels (spec + plan), mutation text, phases (from baseline_done, scenario_done and the copied summary), per-pair roles.
**Not in the journals, so omitted (nothing invented):** indexer events for systems that report none, poller lag for systems other than Setup B, any event whose journal record has no time. System labels / config_label are fixed in `labels.py`, not journaled.
**Gaps / decisions open:**
- Schema status enum has no NOTHING_TO_REVOKE / NO_DATA: the S1-spgroup bundle is skipped loudly (exit code 3). Needs schema v1.1 or a mapping decision.
- Bundles hold all reads of the tracked pairs to the end of the journal (control pair is shared), so short scenarios span about 58 min and bundles are 0.2-1.7 MB.
- An indexer run that started but never finished is exported with status "unfinished" (our choice; not in a journal as such).
- S6 (agent) journals cannot be expressed in schema v1.0.
- Only run 1 exists in runs/benchmark_v2 so far: re-export after the overnight runs (n=7).

## Replay schema v1.1, S6 bundles, write-up fill (Prompt 24; 2026-10-19)

Files: `docs/schemas/replay_bundle.schema.v1.1.json` (v1.0 file kept, new exports are 1.1 only), `src/leaklab/replay/{export,agent,writeup}.py`, frontend `optimus-main/`.

**Verified on the real data (runs/benchmark_v2, run 1 only so far):**
- NOTHING_TO_REVOKE is a status of its own. S1-spgroup now exports: Azure AI Search is NOTHING_TO_REVOKE (never served the SharePoint-group document), SharePoint 6.4 s, Setup B 9.8 s. All 5 scenarios export (S4: NOT_APPLICABLE for all three systems).
- Window trimming (baseline start .. last served read of a flipped pair or conformance time + 60 s, capped at the poll cap): S1-direct 1.0 MB -> 0.5 MB; S1-entra and S2 are unchanged in length (SharePoint serves the file until about 55 min). The summary is still recomputed on the full journal and asserted equal.
- `last_served` per system (S1-entra: SharePoint 54 min 50.9 s after the change, Azure AI Search 1 min 27.1 s; first denials 45 min 43.4 s and 1 min 33.3 s: the gap is flapping).
- 36 S6 trial bundles (4 scenarios x 3 arms x 3 trials). Every report row equals `score_trial` on the journal. Naive arms leaked 18 of 18 trials, the guarded arm 0 of 9, the positive control was served 9 of 9, no trial INVALID. Bundles hold no answer texts, drive item ids, user object ids (`entry_owner`) or per-item guard results.
- Caveats come from the run metadata (tenant count is 1 because `assert_test_tenant` allows exactly one tenant id); the frontend contains none (a test fails if the old wording comes back).

**Choices to review:**
- Representative replicate = smallest sum over systems of |first_deny_s - median| / max(median, 1 s); a replicate that did not converge counts at the cap; systems with nothing to revoke are left out. The formula is written into `index.json` (`ranking.metric`).
- Phase and mutation events are never trimmed, only reads, indexer runs and poller lag.
- With one replicate every scenario's only replicate is "representative": the ranking means something only after the n=7 runs.
- `figures.architecture`, `meta.publish_date`, `repo_url`, `contact` are TODO in `docs/writeup/meta.yaml`; the fill fails until they are set (use `--allow-unset-meta` for a dry run).
- `meta.stale_after_s: 30` is Setup B's `StateReader` default, not read from the deployed service. UNVERIFIED: check the deployed value.

## Where the S6 leaks came from; tenant-guard audit (Prompt 25-2; 2026-10-19)

**S6, path of every leaking turn (journal s6_20261018T150648Z, 18 leaked trials, one leaking turn each).** Checked from the journal
(the oracle's expectation at the end of the turn; retrieval item ids mapped to canaries from the turns where the answer came from
retrieval alone, 14 ids, no conflicts):

| Scenario | Arm | Leaked trials | Path of the leaking turn | Did the asker's own retrieval run on that turn? |
|---|---|---|---|---|
| S6a | naive A (Azure AI Search), naive B (revocation layer) | 3 + 3 | cache_hit | no: the cache answered first |
| S6b | naive A, naive B | 3 + 3 | cache_hit | no |
| S6c | naive A, naive B | 3 + 3 | memory_hit | yes; it did not return the leaked document |

- No leaked canary was delivered via retrieval. In every one of the 36 trials, every retrieval that ran returned only documents the
  asker could read at that moment (by the oracle).
- **What this does not show:** in S6a and S6b retrieval was not re-run on the leaking turns, so the trials cannot say whether Azure
  AI Search would have returned the revoked document then. For S6b that is plausible (direct-grant changes wait for the next
  indexer run, S1-direct), and it was not tested. The wording "retrieval returned only permitted documents" was therefore
  removed from the write-up template, the video script and meta.json; they now say where the leaked content came from, with
  counts computed from the bundles (`S6.naive.leaks_via_cache_memory`, `leaks_via_retrieval`).
- Also removed from the write-up: "switching the cache off alone didn't either" (not tested; replaced by "I did not test turning
  the cache off").

**Tenant guard: every entry point that writes to the tenant.** `config.assert_test_tenant` is called with the token's `tid`:
- `generator/cli.py` `open_env()` (via `guard_test_tenant()`, on the Graph and the SharePoint app token) for `generate`, `verify`,
  `teardown`; `runner/run.py` (benchmark runs, mutations, index cleanup, Setup A provisioning), `agent/s6/live.py` (S6 mutations),
  `setup_b/recall.py` and the probes `delta`, `group_delta`, `index_cleanup`, `mutation`, `search_acl`, `search_index_b`,
  `user_grant_400` all go through `open_env`.
- Direct `assert_test_tenant` calls: probes `access`, `consistency`, `graph`, `persona`, `revocation`, `search_provision` (also on the
  search token), `sp`, `token`; `setup_b/state/run.py` calls `guard_test_tenant()` (it only reads).
- **Exceptions (no guard call, no tenant writes):** `setup_a/*` and `setup_b/*` library modules (they are reachable only from the
  guarded callers above; `setup_a` has no entry point of its own); the Setup B API server `setup_b/api` (it reads Graph and the
  index and rejects user tokens whose `tid` is not its configured tenant, a different check; it writes only its local decision
  log); the replay exporter and the write-up fill (they read local files). `scripts/overnight_benchmark.ps1` only runs the
  guarded benchmark. The guard checks the Graph and SharePoint app tokens; Azure Search writes are covered through the same
  entry points, and `probes/search_provision.py` checks the search token as well.
