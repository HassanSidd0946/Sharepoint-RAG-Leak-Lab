# CLAUDE.md: Leak Lab replay frontend (`optimus-main/`)

The backend rules in `../CLAUDE.md` still hold (every claim comes from a real run; secrets never in files). This
file is for the Next.js app that plays the benchmark back.

## What this app is
A static viewer. It reads files from `public/` and calls no backend. Bundles and `index.json` come from
`uv run python -m leaklab.replay export --runs runs/benchmark_v2 --out optimus-main/public/replays`.
`lib/replay/` (schemas, validation, types, loaders, model, playback) is the contract with the exporter.

## Rules for every change (from Prompt 25)
- **The replay app is the base.** Template code (optimus landing, pulse-metrics, beam-main) is copied in as
  presentational components only; the plan is `../docs/UI_REUSE_PLAN.md`.
- **Components never contain data.** Every number comes from a bundle, `report.json` or `summary.json`; text and links
  come from `meta.json`. `meta.json` holds no result numbers. A link that is `null` hides its component; there is no
  "coming soon".
- **`lib/replay/` (schema, validation, types) is not modified by UI work.** UI helpers go in `lib/ui/`.
- **Nothing on screen may claim something that is not true:** no "Live" unless a live stream is connected, no
  customers, no certifications, no third-party logos.
- **Every existing test keeps passing; the no-mock guard only ever gets stricter.** Today it forbids numeric literals
  in component files (except 0, 1, 2, 100), numbers with units in plain strings, `mock|sample|lorem|faker`, and the old
  fixed caveat wording. Animation and layout numbers live in `lib/ui/*.ts` or `lib/replay/layout.ts`.
- The UI never computes a verdict: it renders `outcome`, `expected` and `verdict` from the bundle. `error` and
  `not_indexed` are grey, never red. NOTHING_TO_REVOKE is neutral grey, never red or green. An INVALID agent trial
  is never shown as OK.
- Any cell with fewer than 3 replicates is labelled "provisional (n = X)" from the data; every cell shows its n.

## Design decisions (Prompt 25-0 answers)
- One font set: Geist and Geist Mono from the `geist` package (bundled, no network at build). No `next/font/google`.
- No `motion` dependency: animated beams are plain SVG; timing is deterministic (constants in `lib/ui/`).
- No canvas decorations (sphere, tetrahedron, wave).
- Charts: recharts, Pulse card style. First-deny chart is a scatter with a log time axis, one dot per replicate,
  DID_NOT_CONVERGE drawn at the cap with its own marker and a legend entry; any jitter is seeded by replicate id.
- `/` (Findings) is outside the AppShell with its own minimal top bar; Replay, Agent, Results and Method are inside it.
  Presentation mode hides the AppShell completely (`components/shell/shell-visibility.tsx`).

## Commands
```bash
npm run dev            # http://localhost:3000
npm run typecheck      # tsc --noEmit
npm test               # vitest (includes the no-mock guard)
npm run build
npm run gen:types      # regenerate lib/replay/types.generated.ts from lib/replay/schema.v1.1.json
```
