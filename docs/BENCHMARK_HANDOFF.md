# Leak Lab: benchmark results and technical issues (handoff for the Principal Architect)

Prepared 2026-10-18. Source: `runs/benchmark/report.json` and `report.html`, built from the five run journals `run_20261018T*_r1..r5.jsonl`. Every number below is read from that report.

## 0. What was run

- Five runs, each on a freshly generated tenant, all three systems polled in the same loop. Read interval 5 s; "converged" = 10 conforming reads in a row; poll cap 60 min after the change. One tenant, one SharePoint site, 45 documents, 7 persona accounts.
- Replicates: S1-direct x3, S1-spgroup x3, S1-entra x5, S2 (nested removal) x5, S4 (move) x2. S1-entra's baseline alternated 1 min / 10 min by run.
- Systems: `setup0` = SharePoint itself (reference, no search). `setup_a_basic` = Azure AI Search SharePoint indexer with ACL ingestion, plain configuration (no SharePoint-group support, 5-minute indexer schedule). `setup_b` = our API in front of a separate, unfiltered index over the same data source.
- **Label that belongs next to every Setup B number:** Setup B queries a separate, unfiltered index (`ll-sp-index-b`) over the same SharePoint data source; Setup A uses the ACL-filtered index (`ll-sp-index`).

## 1. Results: median time until the first denial after the change (seconds)

| Scenario | Setup 0 (SharePoint) | Setup A basic | Setup B |
|---|---|---|---|
| S1-direct (n=3) | **5** (4-17) | **227** (175-240) | **17** (4-20) |
| S1-spgroup (n=3) | **6** (6-35) | never served the documents (3 of 3 runs had nothing to revoke) | **11** (6-35) |
| S1-entra (n=5) | **2565** = 42.8 min (1642-2702); 1 of 5 did not converge within 60 min | **164** (24-257) | **10** (6-19) |
| S2 nested removal (n=5) | **2663** = 44.4 min (1234-2981) | **234** (129-317) | **9** (5-18) |

Leak exposure (how long the revoked document was still served after the change, median / max):

| Scenario | Setup 0 | Setup A basic | Setup B |
|---|---|---|---|
| S1-direct | 0 / 0 | 170 / 182 | 0 / 5 |
| S1-spgroup | 0 / 0 | n/a | 0 / 7 |
| S1-entra | 2933 / 3534 | 106 / 254 | 0 / 8 |
| S2 | 3494 / 3512 | 228 / 312 | 0 / 13 |

S4 (move into a restricted folder, no access change expected): no drift in any system (Setup 0 and Setup A OK in both runs); Setup B's "lockout" verdict there comes from Issue 1 below, not from the move.

### How to read this honestly

- **Setup B revoked in 9-17 s in all four scenarios** (medians 17, 11, 10, 9; worst single replicate 35 s, in S1-spgroup). Its largest leak exposure was 13 s, and none of its replicates failed to converge. That is the poll interval (5 s) plus the Graph delta lag measured earlier (about 5-9 s for drive changes, 11-24 s for group changes).
- **Setup 0's 43-44 minute lag applies to the two Entra-group scenarios only (S1-entra 42.8 min, S2 44.4 min).** For direct and SharePoint-group grants Setup 0 is *faster* than Setup B (5-6 s versus 11-17 s). The Entra case is where plain SharePoint is slow, and where Setup B is roughly 250-300 times faster.
- Setup A basic sits in between for group changes (about 2.7-3.9 min: the query-time group-membership cache, plus the 5-minute indexer for direct grants) and cannot serve SharePoint-group grants at all.
- **Sample sizes are 2-5 replicates per cell, one tenant.** Min/median/max are descriptive; no significance claims. Setup B's measured edge over Setup 0 on Entra grants is far larger than the spread, but the spread itself is not well estimated.
- Read errors (reads that returned no answer, mostly while indexers catch up after a regeneration) are excluded from every number. Setup A had the most (37-127 per cell).

## 2. Technical Issue 1: Setup B start-up lockouts

**What happened.** At the start of runs 1-3, Setup B withheld documents it should have served. Pairs were first served 6-12 s (run 1), about 90-98 s (run 2) and 25-31 s (run 3) after the run began; runs 4 and 5 had none. Direct user grants (p-ceo and p-hr on the special file, p-plateng on S1-Direct) were affected as well as group grants. `/health` never reported degraded, so the state was fresh by the 30 s rule but incomplete.

**Why it does not affect revocation times.** These reads happen before the change, during the pre-change baseline, and Setup B's baseline for every scenario was judged stable on its last reads before mutating. The revocation numbers in section 1 are measured after the change. But the reads are real lockouts, so the report shows a "LOCKOUT" verdict for Setup B in several cells (including S4), and they mean a freshly generated tenant is not served correctly for up to roughly 100 s.

**Cause: unverified.** Working hypothesis: the first sync runs right after `generate`, when SharePoint/Graph have not yet made the new permissions and memberships fully visible; a later poll or the 60 s full verification completes the picture (run 2's ~90 s fits a verification cycle). Not yet checked.

**Proposed fix (small).**
1. Log the first sync's grant and edge counts and compare with a later poll, to confirm or refute the hypothesis.
2. Add a settle wait after generation before building Setup B, and/or make Setup B report "not ready" (and the adapter wait) until two consecutive polls plus one verification show no changes.
3. Optionally exclude pre-baseline reads from the lockout statistic in the report (they are an artifact of start-up, but keep them visible as a separate "start-up" number rather than hiding them).

## 3. Technical Issue 2: stale documents in the index

**What happened.** `ll-sp-index-b` holds **268 documents for 45 live ones**: every canary exists in up to 6 copies, one per tenant generation. Each benchmark run tears the tenant down and regenerates it, and neither indexer removes the documents of a deleted tenant.

**Effect.**
- **No leaks.** Setup B's post-filter decides from its own state, and the stale copies carry item ids the state does not know, so they are denied (`no_such_document`).
- **The recall-cost metrics are contaminated.** The k x 5 over-fetch window fills with denied stale copies, so the recall figures in the report (for example k=10: 1,340 returned of 2,436 possible, when a window of 50 should cover the whole 45-document corpus) overstate the cost of post-filtering. Do not quote them.
- Setup A's index (`ll-sp-index`) very probably has the same stale copies (not counted yet). Their stored ACLs name principals of deleted generations, which is the likely reason they were never served, but that is not verified.

**Proposed fix.** A cleanup step after each teardown that deletes the documents of earlier generations from both indexes (or resets the indexes and re-runs the indexers), followed by a re-measurement of recall on a clean index. It writes to the Azure search indexes, so it needs approval. A full benchmark re-run is not required for the revocation times, only for the recall figures.

## 4. Decision requested from the Architect

Which should come next?

- **Option A: fix first.** Implement the two fixes (start-up readiness and index cleanup), re-measure recall on clean indexes, and update the report. Roughly a small, bounded piece of work. Recommended because the recall numbers are currently wrong, the start-up lockouts show up as a LOCKOUT verdict for our own system, and both would need to be explained to any reader of the report.
- **Option B: proceed to the next phase now** (Scenario S6 cached-answer test, or the dashboard) and carry the two issues as documented caveats.

Also worth a decision: whether the benchmark should also report a start-up number separately, and whether 2-5 replicates are enough for the claims we intend to make, or whether more replicates of the S1-entra / S2 arms are wanted before publication.

## 5. Standing caveats (all in the generated report)

Single tenant and site; 45 documents; 2-5 replicates per cell; `setup_a_basic` is the plain configuration, not a tuned Setup A; Setup B uses its own unfiltered index with its own indexer cycle; the tenant admin is out of scope; times have a 5 s resolution; a non-converged replicate is censored at the 60 min cap.
