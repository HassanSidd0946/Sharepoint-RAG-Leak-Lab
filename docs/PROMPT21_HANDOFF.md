# Leak Lab: Prompt 21 handoff for the Principal Architect

Prepared 2026-10-18. Sources: `docs/FINDINGS.md` ("Fixes after the first benchmark"), `runs/benchmark_v2/report.json` and `report.html`, the run journal `runs/benchmark_v2/run_20261018T114324Z_r1.jsonl`. Full suite: 662 passed, 4 live tests skipped; `ruff` and `mypy --strict` clean. Nothing is committed yet.

## 1. Prompt 21 execution

| Item | Status | Evidence |
|---|---|---|
| Per-generation canaries | Working | Canaries are `CANARY-G<6>-<code>-<nn>-<hex>`; the generation is in the text and in the hash. The id is derived from the creation time of the tenant's container folder (Graph returns `createdDateTime`, verified live). The live run adopted generation `GTN3PY6`. Tests: 5 generations, pairwise disjoint canaries; a pattern without `{generation}` is refused. The oracle needed no change (it gets the generation through the spec). |
| Presence checks match the current item id | Working | Setup A and Setup B count a hit only if its Graph item id is the canary's current one. Tests cover a stale copy with the same text. No stale copy was seen in the clean run. |
| Index cleanup | Done, both indexes | Before: `ll-sp-index-b` held 268 documents (45 current, 223 stale). Delete-by-key works with permission filtering on. 223 stale documents were deleted from each index (1 by the single-key probe, 222 by the full cleanup), 0 failures; both indexes then held exactly the 45 live documents. Cleanup now runs before every run, and the journal records before/after counts (the run itself removed another 45 from the previous generation). |
| `/ready` gate | Working | 200 only after two quiet polls per poller and one completed verification. The runner waits for it (5-minute cap) before the baseline and journals `time_to_ready`. Live: ready 76.6 s after the build started, `prepare()` found it already ready. |

Planted-bug checks: 4 bugs planted in the presence, readiness and cleanup logic, all caught by the new tests.

## 2. Benchmark verification (one clean run, all scenarios n=1, all three systems)

- **Setup B had 0 lockouts in every scenario and 0 read errors.** The 13 readable pairs were first served within 10 s of the first read (the first benchmark: up to 98 s).
- First denial after the change (seconds): S1-direct 18, S1-spgroup 10, S1-entra 10, S2 16 (Setup 0: 18 / 6 / 2743 / 2954; Setup A basic: 298 / never served / 93 / 207). Setup B's leak exposure was at most 9 s.
- **Recall from the clean index: 665 of 665 at k=10 (0 lost).** At smaller k the fixed k x 5 window still costs results: k=3 returned 169 of 236 possible (67 lost), k=5 returned 325 of 392 (67 lost). These are genuine post-filtering costs; the first benchmark's recall numbers were inflated by stale copies and must not be quoted.
- Caveats: n=1 per scenario (observations, not distributions); one tenant; `setup_a_basic` is the plain configuration; Setup B queries a separate, unfiltered index over the same SharePoint data source, while Setup A uses the ACL-filtered index.

### Open point

The start-up lockouts of the first benchmark are gone with the gate, but their cause is **not** explained. The first sync at start-up was already complete (same counts at readiness, 0 drift found), so the hypothesis "incomplete first sync" is not supported. The cleanup also adds about 45 s between generating the tenant and the first sync, so the gate and that delay cannot be told apart yet. Separating them needs a run with the gate on and the delay removed.

## 3. Overnight schedule

Runs 2 to 7 (S1-entra and S2, baseline alternating 10 / 1 / 10 / 1 / 10 / 1 min) were appended to the same benchmark set (`runs/benchmark_v2`); after the finished run 1 they are all pending. Three Windows scheduled tasks, 22:00 local, two runs each:

| Task | Date | Runs |
|---|---|---|
| `LeakLab-Benchmark-Night1` | 2026-10-19 | 2, 3 |
| `LeakLab-Benchmark-Night2` | 2026-10-21 | 4, 5 |
| `LeakLab-Benchmark-Night3` | 2026-10-23 | 6, 7 |

All three show state Ready. Needs the PC on and awake (the tasks do not wake it), the user logged in, and Docker Desktop running. Each run tears the test tenant down and regenerates it. Details and cancel commands: `docs/OVERNIGHT.md`.

## 4. Request: next phase, Scenario S6 (agent cache / memory)

Please provide the exact prompt for S6. What exists today, so the prompt can build on it:

- `tenant_spec.yaml` has the placeholder `S6-cached-answer` (no mutations): "p-finlead's cached answer about Finance/Leadership must not reach p-intern."
- The runner only supports scenarios that change the tenant; S6 is a cache test and needs its own driver. `src/leaklab/agent/` is still empty (the `agent/` package in the layout was planned for a LangGraph agent with cache/memory).
- Setup B has the decision log, the authoritative state (membership and ACL stores) and `/explain`; the fail-closed rule says a cache may return content only after re-checking the current user's access to every source document.
- Leaks are detected by exact canary-string match, never by an LLM judge; the oracle stays independent.

Questions for the Architect: which agent framework and model (or a deterministic stub) should S6 use; what the cache key and memory scope are (per user, per group, shared); what counts as a leak (canary text from a source the asking user cannot read, in the answer or in a cached/memory entry); how S6 relates to the three systems under test (setup0 has no cache, setup_a_basic and setup_b need an agent in front of them); and whether the oracle needs a new notion ("who may see this answer") derived from the source documents.
