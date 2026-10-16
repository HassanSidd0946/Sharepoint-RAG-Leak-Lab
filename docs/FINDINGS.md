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
- Next experiment (now supported by the probe): `--suffix NAME` gives the folder, file and groups new
  names; every read cycle also records SharePoint's own effective-permission answer for the user
  (`GET .../getusereffectivepermissions`, UNVERIFIED endpoint), which shows whether SharePoint itself
  thinks the user can read, independently of the Graph read path.
- Probe output: probes/out/revocation_probe.json (status `failed`: it stopped at the timeout; the
  data are complete)
- Docs: n/a

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
- **Timing.** Membership became visible after about 12s in two samples, a license after up to
  about 13s, and a new member could read after 10-17s in two trials but still got 403 after 30
  minutes in later runs (Entra listed the member throughout). Removing a member was NOT reflected in real
  reads for at least about 15 minutes (2 trials, old and fresh tokens alike; see "Revocation"). So
  "who can read right now in SharePoint" lags the directory state by a window whose upper bound is
  unknown. The oracle should not encode a time window until that bound is measured, and the question
  of what the oracle should compare against (directory state vs. real reads) is open.
- **Licensing.** Only licensed personas were tested for access.

Still not probed: real READS for the cases above that were only listed (nested folders, moved
file, file-level grant), and everything on revocation timing.

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
- [ ] VARIABLE PROPAGATION of a NEW membership to SharePoint reads: 10-17s in two trials, then never
  within 306s and 1805s in two later runs (Entra listed the member). What decides it (a brand-new group, the `ensureuser` retry, the
  first access by that user, something else)? The brand-new-user test (denied for 305s) did not support the "per-user
  view fixed at first access" hypothesis. Next: `--suffix NEW` (new folder/file/group names) and read
  the SharePoint effective-permissions column.
- [ ] REVOCATION TIMING (the core question for S1/S2/S6). Direct membership removal: still allowed
  after 15 minutes, old and fresh tokens (see above); no upper bound yet. To do: (1) a long, sparse
  poll (hours) to find when, or whether, it turns 403; (2) re-run with the Entra check now in the
  probe to confirm Entra dropped the membership early; (3) nested removal (`--mode nested`, S2);
  (4) removing the folder's role assignment instead (S1); (5) another read path, e.g. SharePoint REST
  with a SharePoint-audience token or `GetUserEffectivePermissions`, to see whether the delay is in
  SharePoint or in the Graph content path.
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
