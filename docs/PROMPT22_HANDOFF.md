# Leak Lab: project completion handoff (Prompt 22) for the Principal Architect

Prepared 2026-10-18. Sources: `docs/FINDINGS.md`, `runs/benchmark_v2/report.html` and `report.json`, the journals `runs/benchmark_v2/s6_20261018T150648Z.jsonl` (S6) and `run_20261018T114324Z_r1.jsonl`, `probes/out/recall_20261018T135534Z.json`, and the earlier handoffs. Full suite: 737 passed, 4 live tests skipped; `ruff` and `mypy --strict` clean.

## 1. Adaptive paging: recall on the clean index

The fixed k x 5 over-fetch is replaced by paging: pages of max(k, 10) hits are fetched and decided until k results are allowed, the index is exhausted, or 10 pages are used. Pages fetched are journalled. Measured live (7 personas, broad query, clean index, 45 documents):

| k | adaptive paging | old fixed k x 5 window |
|---|---|---|
| 3 | 18 of 18 possible, **0 lost** (2.4 pages on average, 5 at most) | 13 of 18 (5 lost) |
| 5 | 30 of 30, **0 lost** (3.0 pages) | 25 of 30 (5 lost) |
| 10 | 51 of 51, **0 lost** (3.9 pages) | 51 of 51 |

No query reached the page cap. Scope of the claim: one tenant generation, 45 documents, the 7 personas, ties in the index ranking assumed stable across pages (observed, not documented by the service). The price is more index round trips for personas who can read little.

## 2. S6, agent cache and memory: the result

**What the leak is.** The leak is in the *agent layer's shared cache and memory*, not in Setup A's or Setup B's retrieval: both return only documents the user may read. A naive agent that caches answers and remembers facts for all users, with no permission metadata, hands one user's content to another. "Naive A" and "naive B" below differ only in which system supplies the retrieval. The guarded agent stores the source item ids and tenant generation with every cache entry and memory item and, on every hit and before every LLM call, re-checks that the CURRENT user may read EVERY source against Setup B's live state (not the index ACL fields). Anything unknown is a denial.

Live run on a fresh tenant generation, 3 trials per cell, all 36 trials valid. A leak = a canary delivered to a persona whose expected access at that moment (the independent oracle) lacks it. Detection is exact canary-string match.

| Scenario | Naive agent, Setup A retrieval | Naive agent, Setup B retrieval | **Guarded agent, Setup B** |
|---|---|---|---|
| S6a: cross-user cache (a finance lead's cached answer reaches an intern) | **LEAK 3 of 3** | **LEAK 3 of 3** | **OK 3 of 3** (9 guard denials) |
| S6b: cache after a direct-grant revocation (the revoked user still gets the cached answer) | **LEAK 3 of 3** | **LEAK 3 of 3** | **OK 3 of 3** (6 guard denials) |
| S6c: shared memory (a different question recalls another user's remembered facts) | **LEAK 3 of 3** | **LEAK 3 of 3** | **OK 3 of 3** (3 guard denials) |
| Positive control (both users may read the source) | served 3 of 3 | served 3 of 3 | **served 3 of 3** (no over-blocking) |

What the guarded arm does and does not do: in every attack trial it faced a real cache or memory candidate and refused it; it delivered nothing it should not. It still STORES such answers (its "exposure surface" is as large as the naive arms'), so the guard controls delivery, not storage. A further finding from the tests: switching the cache off alone does not make a naive agent safe, the shared memory leaks the same text.

### The validity bug found and fixed (Setup A, S6b)

The first live run scored the naive Setup A arm of S6b as "OK" (no leak). That was an invalid result, not a pass: Setup A had not yet indexed the S1-Direct documents, so its retrieval answered with other documents the user may still read, and the cached answer held nothing that could leak. The baseline wait had only waited for "any result". Fixes:

1. **Source wait:** the (versioned, fixed) query set now names the document each seed question must retrieve, and the driver waits until the arm's own retrieval returns that document.
2. **Validity rule:** a cache or memory hit only counts if the seed answer holds a canary the asker may not (or, for S6b, no longer) read; otherwise the trial is INVALID, never OK. Applied to the guarded arm as well, so "no leak" is only evidence when the guard actually faced a candidate.

Re-scored under the new rule, the old journal gives INVALID for exactly that cell and nothing else changes; the corrected live run shows naive Setup A leaking in S6b like the others. The tests plant 9 bugs (guard skipped, one source unchecked, shared thread readable, INVALID counted as OK, memory guard skipped, context guard skipped, generation not checked, delivery time ignored, thread history not enforced), and all are caught.

S6 caveats (generated into the report): the LLM is a stub that quotes retrieved, cached and remembered text verbatim (the worst-case leaker), so results measure the cache, memory, retrieval and guard, not a model; the embedding is a deterministic hash embedding with fixed thresholds, so other embeddings change how often a naive agent leaks; one tenant; 3 trials per cell are not independent draws; setup0 is not applicable; Setup A basic retrieval is frozen until its indexer runs; the guard relies on Setup B's state, which lags the tenant by seconds (S6b waits 30 s after the confirmed revocation).

## 3. The core numbers (recap): time until a revoked user is first denied

Median seconds after the change was confirmed. Read interval 5 s; one tenant; 45 documents; only the 7 test personas (the tenant admin is out of scope).

| Scenario | SharePoint itself | Azure AI Search, plain indexer ACL setup ("setup_a_basic") | **Setup B (our layer)** |
|---|---|---|---|
| Entra-group member removed (S1-entra, n=5) | **2565 s, about 43 min** (1 of 5 did not converge in 60 min) | 164 s, about 3 min | **10 s** |
| Nested Entra group removed (S2, n=5) | **2663 s, about 44 min** | 234 s, about 4 min | **9 s** |
| Direct grant removed (S1-direct, n=3) | 5 s | 227 s | 17 s |
| SharePoint-group member removed (S1-spgroup, n=3) | 6 s | never served the documents (the plain configuration does not honor SharePoint groups) | 11 s |

Headline, stated precisely: for **Entra-group changes** SharePoint takes about 43 to 44 minutes, Azure AI Search about 3 to 4 minutes, and Setup B about 10 seconds (the largest leak window we saw for Setup B was 13 s). For direct and SharePoint-group grants plain SharePoint is *faster* than Setup B (5 to 6 s against 11 to 17 s), so the claim must stay scoped to Entra groups. After the fixes, the verification run confirmed Setup B had no lockouts in any scenario; a clean index also removed stale documents (268 held versus 45 live) that had inflated the first recall numbers.

Standing caveats: 2 to 5 replicates per cell, one tenant; "Azure AI Search" here is Microsoft's SharePoint indexer with ACL ingestion in its PLAIN configuration (users and Entra groups only, smallest 5-minute schedule), not a tuned setup; Setup B queries a separate, unfiltered index over the same SharePoint data source, while Setup A uses the ACL-filtered index. Still open: the cause of the start-up lockouts seen in the first benchmark (gone with the readiness gate, not explained). Six more S1-entra and S2 replicates are scheduled overnight (2026-10-19, 21, 23), which will take those cells to n=7 in the same report.

## 4. Request: next phase, the write-up, outreach and the 2-minute video

The measurement work is complete. Please provide, based on these concrete findings:

1. **The write-up structure**: title and thesis, the sections and their order, which figures and tables carry the argument (the three-system timeline for Entra-group revocation, the S6 table), where the method and caveats go, how to present the honest limits (n of 2 to 5, one tenant, plain configuration, stub LLM, hash embedding, direct grants where SharePoint wins) without burying the result, and what to publish alongside it (the report, the journals, the findings log).
2. **The 2-minute video script**: a shot-by-shot script with timings and on-screen text built from the numbers above (for example, one Entra-group removal and the three clocks; then the S6 leak and the guard).
3. **Outreach**: whom to target and the message to send; how to word the claim so it is accurate (the S6 leak is in the agent's shared cache and memory, not in the search products' own filtering; the 43-minute figure is for Entra-group changes).
4. **Decisions only you can make**: whether to wait for the overnight replicates (n=7) before publishing; whether the write-up should name Microsoft products and their configuration as measured; and what must be re-run or double-checked first (for instance the Setup A restore delay of 272 s in S6b, which is consistent with its indexer cycle but not confirmed from the indexer events, and the real-LLM exploratory command, which was never run against the live API).
