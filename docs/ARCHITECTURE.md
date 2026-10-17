# Architecture and threat model

Written for the Setup B API (prompt 19b). Facts about Microsoft services are in `docs/FINDINGS.md`;
this file says how our parts fit and what they must and must not be trusted with.

## The three systems under test

| System | What serves the persona | Gate |
|---|---|---|
| `setup0` | SharePoint itself (no search) | SharePoint |
| `setup_a_basic` | Azure AI Search index `ll-sp-index` (SharePoint indexer with ACL ingestion) | the service, per query, from the stored ACL and the user's token |
| `setup_b` | our API over index `ll-sp-index-b` | **our post-filter only** |

**Setup B queries a separate, unfiltered index over the same SharePoint data source; Setup A uses
the ACL-filtered index.** (This sentence is also in every run report that includes either system.)

Why two indexes: the service identity sees 0 documents in an ACL-enforcing index (verified,
FINDINGS "Setup A probes"), so Setup B cannot post-filter there. `ll-sp-index-b` was created from the
same body as `ll-sp-index` with only `permissionFilterOption` changed, and a second indexer on the
same data source with the same schedule (FINDINGS "Setup B: service API"). Caveat: the two indexers
run on their own 5-minute cycles, so freshness of content is comparable, not simultaneous.

## Setup B data flow

```
persona token ──► POST /query ──► validate token (signature, issuer, tenant, audience, user token)
                                   │
                  service token ──►│ search ll-sp-index-b: top k*5, UNFILTERED       (search.py)
                                   │
   Postgres: membership_store ─┐   │ for every hit: allow/deny from the state        (access.py)
              acl_store ───────┴──►│   (acl_store x membership_store, nothing else)
                                   │
                                   ├─► decision log (hash chain, Postgres)  -- must succeed
                                   └─► first k allowed hits (id, name, content)
```

Pollers (every 5 s, separate threads): Graph group delta -> membership_store; Graph drive delta +
SharePoint REST -> acl_store; a full ACL verification every 60 s. All state in Postgres.

## What decides access

Only `StateReader` (`setup_b/state/access.py`), from `acl_store` and `membership_store`.
`UserIds`/`GroupIds` in `ll-sp-index-b` are stale snapshots from the indexer. **No decision reads
them.** `/explain` may show them next to the decision, for comparison, labelled as such.
A test (`test_explain_shows_the_path_and_the_index_acl_only_for_comparison`) makes the index claim the
opposite of the state in both directions and checks the state wins.

## Fail closed

| Situation | Result |
|---|---|
| bad / expired / wrong-audience / wrong-tenant / non-user token | 401, nothing searched, nothing logged |
| membership poller's last success older than 30 s (or never) | every GROUP-based grant denied; grants naming the user still count; `/health` = degraded |
| ACL poller's last success older than 30 s (or never) | everything denied (direct grants come from the same state); `/health` = degraded |
| user unknown to the directory and in no group | denied (`unknown_principal`) |
| unknown principal type / claim in a grant, Limited Access, unknown role | never grants |
| index key that cannot be mapped to a drive item id | hit dropped, counted |
| index, state store or decision log unavailable | 503, no results |
| decision log cannot be written | 503: **no log, no answer** |
| anything unexpected | 500 `{"error": "internal"}`, no detail |

The spec for 19b says "deny every group-based grant" when a poller is stale. We apply that to the
membership poller and go one step further for the ACL poller (deny everything), because a direct
grant that was revoked in SharePoint would otherwise still be honored from stale state (rule 3 in
CLAUDE.md: stale data denies).

## Threat model: what the unfiltered index changes

`ll-sp-index-b` has no trimming. Anyone who can query it sees every indexed document. So:

1. **Setup B is the only gate.** A bug in the post-filter is a leak, not a degraded result. The
   post-filter has property tests against the oracle (store == oracle after random mutation
   sequences), 8 planted-bug checks for the API layer, and fail-closed tests for every row above.
2. **The service token / admin key stays server-side.** It exists only inside `HttpIndexPort`
   (`setup_b/api/search.py`), in the API process. It is never in a response, a log line, a decision
   record, an error message or the runner adapter. The adapter (`setup_b/system.py`) only holds the
   persona tokens it sends to `/query` and talks plain HTTP to the API; a test asserts the index
   request carries the service bearer and no user header, and that records contain neither tokens,
   query text (only its sha256) nor content.
3. **Who else can read `ll-sp-index-b` directly?** Anyone with a data-plane role on the search
   service (Search Index Data Reader/Contributor, the admin key). In this test tenant that is the
   driver app and the admin. In a real deployment the index must live on a service whose data-plane
   roles belong to the API's identity only, and the admin/query keys must be disabled (`aadOrApiKey`
   -> RBAC only). Not enforced by code; a deployment requirement.
4. **The index contents are the whole corpus.** Network access to the service (public endpoint here)
   is the second line of defence; use a private endpoint outside a test tenant.
5. **Query text** goes to the `search` field only (simple query syntax); there is no user-supplied
   `filter`, `select` or `orderby`. A wildcard query can enumerate documents but only the allowed
   ones are returned. Length limit 1000 characters, k limit 50.
6. **Information leaks through errors and counts.** Responses never include a stack trace, a token,
   content of a denied document or a count of removed documents. The debug stats that do reveal the
   filter's effect (`fetched`, `denied`) are honored only if the server is started with
   `allow_debug` (benchmark runs); `/explain` and `/metrics` are disabled unless an operator key is
   configured and compare it in constant time. They bind to 127.0.0.1; there is no TLS.
7. **Token validation limits.** There is no API app registration (the driver app cannot create
   one), so `/query` accepts tokens with a configured audience, by default
   `https://search.azure.com`: any such token issued to a user of this tenant works. A token is valid
   until `exp` (about an hour) and is not checked for revocation (Continuous Access Evaluation is not
   used), so a stolen token identifies its user until it expires. It cannot widen access: decisions
   come from the state, not from token claims other than `oid`.
8. **The decision log is tamper-evident, not tamper-proof.** Anyone who can rewrite the whole table
   can recompute the chain. Publish the head hash (`verify()` returns it) outside the database
   periodically if that matters.
9. **Availability.** Over-fetching k*5 hits per query and one state read per hit are cheap here (43
   documents) and untested at scale; there is no rate limiting.

## Recall cost of post-filtering

The API fetches `k*5` hits and filters. When the top of the ranking is forbidden, fewer than `k`
come back although readable documents exist further down (test:
`test_post_filtering_loses_recall_when_the_top_of_the_ranking_is_forbidden`). The runner journals this
as `postfilter_recall` events (k, window, fetched, allowed in the window, returned, readable among all
matches) and the report summarizes it. Setup A's service-side filter does not have this cost.

## Not covered / open

See `docs/FINDINGS.md`, "Limits and open questions" of both Setup B sections.
