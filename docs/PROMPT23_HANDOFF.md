# Prompt 23 Handoff: Replay Exporter and Dashboard

**To:** Principal Architect (Opus 5.5)  **From:** Lead Enterprise Developer  **Date:** 2026-10-19
**Status:** 23a and 23b delivered. Waiting on the overnight runs (n=7) before the final recording.

## 1. Prompt 23a: Replay Exporter (`src/leaklab/replay/`)

- **Schema:** Every bundle is validated against `docs/schemas/replay_bundle.schema.json` (v1.0, `jsonschema`) before it is written. The schema file now lives in `docs/schemas/`.
- **Verdicts:** `expected` and `verdict` per read come from the oracle (`IntentTimeline` over the run's mutation log). `error` and `not_indexed` reads are "unscored", never "denied". `t_rel_s` is relative to the mutation's `confirmed_at`.
- **Summary check:** The summary is copied from `report.json` and also recomputed with `oracle.scoring.classify`. The export aborts if they differ. For all 4 exported scenarios and 3 systems the two are equal.
- **Redaction:** Bundles hold no tenant ids, item ids, UPNs, emails, URLs or admin data. Personas are id plus fictional display name, and documents use `doc_label`. A scanner (GUID, `@`, `sharepoint.com`, `onmicrosoft`, URL, drive ids) runs on every bundle and on `index.json`. An independent regex scan of the output files was also clean.
- **Provenance:** Each bundle records journal file, report file, exporter version, git commit and `exported_at`.
- **Output:** 4 bundles plus `index.json` are in `runs/replays/`, produced from `runs/benchmark_v2`: S1-direct, S1-entra, S2-nested-group-removal and S4-move-into-restricted.
- **Figures:** `python -m leaklab.replay figures` makes the three-lane timeline and the first-deny dot plot (SVG and PNG) from the same bundles. Captions are generated from bundle data.
- **Checks:** 13 new exporter tests pass. The full suite is 750 passed and 4 skipped, with ruff clean and `mypy --strict` clean.
- **Not committed:** Nothing from 23a or 23b is committed yet.

### Known gaps (decisions needed)

1. **S1-spgroup is not exported.** Its status is `NOTHING_TO_REVOKE`, which is not in the schema's status enum. The exporter skips it loudly (exit code 3). Options: schema v1.1, map it to `NOT_APPLICABLE`, or leave it out.
2. **Bundles are long and large (0.2 to 1.7 MB).** They keep every read of the tracked pairs to the end of the journal, and the control pair is shared. Short scenarios therefore span about 58 minutes.
3. **Indexer runs that never finished** are exported as status "unfinished". That label is my choice.
4. **S6 (agent) journals** cannot be expressed in schema v1.0.
5. **Two fixed caveats** ("One test tenant", "Tenant admin out of scope") have no schema field. They are fixed strings in the frontend. Suggestion: add a `caveats` list in v1.1.

## 2. Prompt 23b: Frontend (`optimus-main/`, Next.js 16, App Router)

- **Scaffold audit:** The v0 project is Next 16.0.10, React 19.2, Tailwind 4 and shadcn/ui. All mock data was in `components/landing/`.
- **Mock data removed:** The whole landing page and its placeholder assets are deleted. Analytics and unused three/expo/react-native dependencies are also removed, so there are no external calls. `/` now redirects to `/replay`.
- **Data path:**
  - The schema is copied to `lib/replay/schema.json` and the TypeScript types are generated from it (`npm run gen:types`).
  - The bundles are copied to `public/replays/`. Each bundle is checked with ajv at load, and an invalid one shows an error panel.
  - The UI renders `outcome`, `expected` and `verdict` as given and never computes a verdict. Errors and not-indexed reads are grey, never red.
- **Page:**
  - **Routes:** `/replay` and `/replay/[bundle]`, with a bundle picker built from `index.json`.
  - **Lanes:** Three lanes in the order SharePoint, Azure AI Search, Revocation layer, each with its `config_label` caveat.
  - **Marks:** Leak marks, indexer-run markers (Azure lane, when the bundle has them) and a t=0 "change confirmed" line.
  - **Clocks:** Per-lane clocks that freeze red at that lane's `first_deny`, or show "did not stop within cap".
  - **Playback:** Play/pause, a scrub bar, and speeds of 1x, 10x, 60x and 300x. The label "Real run, played back at Nx" is always visible, and the speed in it comes from state.
  - **Other:** Summary strip, provenance footer, a linear/log axis toggle, presentation mode and light/dark themes.
- **Checks:** `tsc --noEmit` is clean, `next build` succeeds, and 55 vitest tests pass. These cover:
  - The component fixture: ticks, grey for errors, leak marks, indexer markers, clocks freezing at first_deny, and the cap state.
  - The invalid-bundle error panel.
  - All 4 real bundles validating against the schema.
  - A guard that fails on numeric metric literals or the words mock, sample, lorem or faker in component files.
- **Browser check:** I ran it at 300x in the in-app browser and the overlay and clocks behaved correctly. I have not made a production-quality screen recording.

## 3. S1-Entra numbers currently on screen

Source: run `run_20261018T114324Z_r1`, bundle `run_20261018T114324Z_r1__S1-entra__r1`, replicate 1 of 1.

| System | First denial after the change | Status | Leak exposure |
|---|---|---|---|
| SharePoint | **45 min 43.4 s** (2743.4 s) | CONVERGED | 54 min 50.9 s |
| Azure AI Search (basic ACL config) | **1 min 33.3 s** (93.3 s) | CONVERGED | 1 min 27.1 s |
| Setup B (revocation layer) | **10.3 s** | CONVERGED | 0.0 s |

- **Caveat:** This is n = 1. Treat the numbers as provisional until the overnight runs finish.
- **SharePoint leak exposure:** SharePoint's leak exposure is longer than its time to first denial. That comes from the run's data, and the UI shows it as given.
- **URL:** `http://localhost:3100/replay/run_20261018T114324Z_r1__S1-entra__r1`, started with:

```bash
npm --prefix optimus-main run dev -- -p 3100
```

## 4. Next steps and requests

1. **Dev (us):**
   - Wait for the overnight runs (n=7) to finish.
   - Re-export with `python -m leaklab.replay export --runs runs/benchmark_v2 --out optimus-main/public/replays`.
   - Re-pick the best S1-entra bundle (the exporter ranks them).
   - Do the final screen recording, with all numbers regenerated from the new bundles.
2. **Architect (you), please verify:** the frontend against schema v1.0 and the rules above. Also give a decision on the five gaps in section 1.
3. **Architect (you), please provide while we wait:**
   - **Draft write-up / article.** Fill the numbers only from the final bundles and report. Do not copy the provisional n=1 values above.
   - **2-minute video voice-over script.** It should match the playback overlay: three lanes, the "change confirmed" line, frozen clocks and the "Real run, played back at Nx" label. Tell us which playback speed and bundle to record.

Suggested commit message for the exporter and frontend work:

```
Add replay exporter (schema v1.0 bundles, redaction, figures) and replay dashboard

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
```
