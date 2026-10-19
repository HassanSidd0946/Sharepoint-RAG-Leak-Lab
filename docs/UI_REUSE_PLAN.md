# UI reuse plan (Prompt 25-0)

Status: **proposal, no code changed.** Git tag `replay-v1` marks the current state (the replay app as of
commit `27ca6e5`; `docs/writeup/meta.yaml` has uncommitted edits that are not part of the tag).

Sources read: `optimus-main` (the replay app; its v0 landing page was deleted in Prompt 23b and is read from
git history, `f63029f^`), `pulse-metrics-main`, `beam-main`, the existing frontend tests (`tests/no-literals.test.ts`
and the others), `lib/replay/`, `PROMPT_25.md`. There is **no frontend CLAUDE.md** yet (only the backend one in the
repo root); 25-1 should create `optimus-main/CLAUDE.md` with the rules at the end of `PROMPT_25.md`.

## 0. What the three templates are

| Template | Stack | What it is | Hardcoded data |
|---|---|---|---|
| optimus (landing) | Next 16, Tailwind 4, Canvas-2D decoration, no heavy deps | 13-section marketing page for a fictional "Optimus" platform | every string and number: stats, features, code samples, cities, metrics, certifications, testimonials, pricing, links |
| pulse-metrics | Next 16, Tailwind 4, recharts | SRE dashboard shell (sidebar, header, right rail) plus 10 section views | `content/*.tsx` arrays, `right-panel.tsx` arrays, header titles, sidebar badges and user. `lib/data.ts` and `lib/types.ts` are leftovers of a different template (research studies, insights, participants) and **no Pulse file imports them** |
| beam-main | Next 15, **Tailwind 3** (`tailwind.config.ts`), **`motion`** package | one animated-beam demo: seven brand-logo circles around OpenAI | six inline third-party brand SVGs, random beam durations |

Cross-cutting facts that shape every decision below:

1. **The no-literals guard stays as it is.** Component files may contain no numeric literal except 0, 1, 2, 100, no
   "number + unit" strings and no mock/sample/lorem/faker. The templates are full of animation delays
   (`i * 50`), `setInterval(…, 2500)`, canvas maths, `Array(8)` grid lines. Every REUSE therefore moves such
   numbers to `lib/ui/*.ts` constants (layout and animation only, never data), exactly as `lib/replay/layout.ts` does.
2. **Data sources** (all static files in `public/`): bundles and `index.json` (exist), `report.json` (to copy; checked
   with the redaction scanner: 0 hits), `summary.json` (**new**, backend export + JSON Schema), `meta.json` (**new**,
   non-measured text and URLs only, no result numbers; a test enforces it). `runs/` is git-ignored, so these are copied
   into `optimus-main/public/` by the export command.
3. **shadcn `components/ui/*`** (57 files per template): pulse's folder is byte-identical to optimus's; beam's is the
   Tailwind-3 variant. The replay app imports **none** of them; the landing code used `Button`, pulse used `Button` and
   `Input`. Plan: keep `button.tsx` only; delete the other 56 (and their Radix and other dependencies) in 25-4.
4. Presentation mode must hide the AppShell completely. It lives inside `ReplayView`, so the shell reads a small
   React context (`components/shell/ShellVisibility`) that `ReplayView` sets; `lib/replay/` is not touched.
5. Template layouts load Google fonts through `next/font/google` and `@vercel/analytics`. Analytics is already gone;
   fonts are decided in 25-1 (question Q4).

## 1. optimus: landing page → `/` (Findings)

Template file → what it renders → its hardcoded content → dependencies → **decision**.

| # | File (git `f63029f^`) | Renders | Hardcoded content | Deps | Decision |
|---|---|---|---|---|---|
| 1 | `navigation.tsx` | floating header, scroll-shrink, mobile full-screen menu | brand "Optimus TM", links Features/How it works/Developers/Pricing, "Sign in", "Start creating" | `Button`, lucide | **REUSE** as `findings/FindingsNav` (strip sign-in, CTA, "TM", pricing link). Data: route/anchor list in code (labels are navigation, not data) + brand from `meta.json` |
| 2 | `hero-section.tsx` | headline with rotating word, eyebrow, 2 CTAs, grid lines, sphere, **stats marquee** | "The platform to create/build/scale/ship", "Your toolkit…", "Start free trial", "Watch demo", marquee: 20 days/98%/300%/6x with NETFLIX/STRIPE/LINEAR/NOTION | `Button`, `AnimatedSphere`, CSS `marquee`, `animate-char-in` | **REUSE** layout as `findings/Hero`; DELETE rotating word, marquee and company names. The marquee becomes `StatsStrip` (row 2b) |
| 2b | (part of hero) stats marquee | scrolling stats | see above | CSS only | **REUSE the look only** as static `findings/StatsStrip` (no scrolling) |
| 3 | `features-section.tsx` | 4 numbered feature rows with animated SVG visuals (deploy/ai/collab/security) | 01–04 titles and descriptions ("Instant Deployment", "SOC 2 compliance"…), 4 SVG animations | IntersectionObserver | **REUSE** list layout as `findings/FindingsList` (three findings); DELETE the 4 generic SVG visuals (they illustrate nothing we measure) |
| 4 | `how-it-works-section.tsx` | dark section, 3 steps (I–III), auto-advance, fake `workflow.ts` code panel with "Ready" dot | step titles ("Connect your tools", "200+ data sources"), `optimus.*` code, "30 seconds" | `setInterval` 2.5 s | **REUSE** as `findings/MethodSteps` with real commands; DELETE the green "Ready" dot (implies a live status); auto-advance interval to `lib/ui` or removed |
| 5 | `infrastructure-section.tsx` | "Global by default", 17 data centers/6 continents, 6 city latency list with highlight cycle | cities, ms latencies, "99% of the world", "Edge Network" + green pulse dot | `setInterval` | **REUSE** list-with-highlight layout as `findings/SystemsMeasured` (three systems); DELETE cities, latencies, stats, "Edge Network", the pulse dot |
| 6 | `metrics-section.tsx` | "Live metrics": 4 animated counters, `LIVE` badge with green dot and **ticking clock** | 2 847 392 requests, 99.99% uptime, 23 ms, 184 countries, `new Date()` clock | rAF counter, IntersectionObserver | **REUSE** `AnimatedCounter` + card grid as `findings/GlanceMetrics`; DELETE "Live", the dot, the clock and all four metrics |
| 7 | `integrations-section.tsx` | two marquees of integration names | 14 product names with categories | CSS marquee | **DELETE** (third-party names, marketing) |
| 8 | `security-section.tsx` | 4 security cards, certification chips | SOC 2 Type II, AES-256/TLS 1.3, zero-trust, "GDPR & HIPAA", chips SOC 2/ISO 27001/HIPAA/GDPR/CCPA | lucide | **REUSE** card grid as `findings/LimitationsGrid`; DELETE all copy and the chips; one card per bundle/report caveat |
| 9 | `developers-section.tsx` | "For devs." with feature list and tabbed code panel (Install/Initialize/Deploy), typing animation, copy button | npm/pnpm commands, `Optimus` SDK code, "TypeScript native", "12KB gzipped", … | lucide, state | **REUSE** as `findings/ReproduceTabs` (Install / Run / Report, real commands); DELETE the feature list |
| 10 | `testimonials-section.tsx` | quote carousel and "Trusted by" logo marquee | 4 invented quotes with names and companies, 8 company names | state | **DELETE** (invented endorsements) |
| 11 | `pricing-section.tsx` | 3 plans, monthly/annual switch | Starter/Pro/Enterprise, prices, feature lists | state | **DELETE** |
| 12 | `cta-section.tsx` | bordered box with mouse-follow gradient and tetrahedron | "Ready to build", "Join thousands of teams", "Start building free", "Talk to sales", "No credit card required" | `Button`, `AnimatedTetrahedron` | **REUSE** box as `findings/ContactCta`; DELETE tetrahedron and all copy |
| 13 | `footer-section.tsx` | 4 link columns, social links, wave animation, "All systems operational" | Product/Developers/Company/Legal links, Careers "Hiring", Twitter/GitHub/LinkedIn, status line | `AnimatedWave` | **REUSE** as `findings/FindingsFooter` (name, GitHub, LinkedIn, write-up, repo only); DELETE columns, "Careers", the status line, the wave |
| 14 | `animated-sphere.tsx` | Canvas-2D point sphere (103 lines, no dependency) | none (maths constants) | canvas | **OPTIONAL KEEP** as hero decoration; constants to `lib/ui/decor.ts`; must respect `prefers-reduced-motion`. See Q5 |
| 15 | `animated-tetrahedron.tsx` | Canvas-2D tetrahedron (175 lines) | maths constants | canvas | **DELETE** (decoration only; each kept decoration costs guard exceptions) |
| 16 | `animated-wave.tsx` | Canvas-2D wave (81 lines) | maths constants | canvas | **DELETE** (same reason) |
| 17 | `app/page.tsx` (landing composition) | assembles the 13 sections | – | – | **REWRITE** as the Findings page composition |
| – | `theme-provider.tsx`, root `layout.tsx` | next-themes wrapper, fonts | Instrument Sans/Serif, JetBrains Mono via `next/font/google`, title "Optimus" | next-themes | theme-provider **already in use**; layout replaced in 25-1 (Q4) |

### Props after stripping (Findings)

```ts
interface Link { label: string; href: string; external?: boolean }

FindingsNav      { brand: string; links: Link[] }                       // + existing <ThemeToggle/>
Hero             { eyebrow: string; question: string; scope: string;
                   actions: (Link & { variant: "primary" | "outline" })[]; decoration?: ReactNode }
StatsStrip       { heading: string; items: { label: string; value: string; caption: string }[] }
                 // value = formatted median from summary.json, caption = "n = …"
FindingsList     { heading: string; items: { number: string; title: string; description: string;
                   figure?: { value: string; caption: string } }[] }
MethodSteps      { heading: string; steps: { number: string; title: string; description: string; code: string }[] }
SystemsMeasured  { heading: string; intro: string;
                   systems: { id: SystemId; label: string; configLabel: string; median: string; n: string }[] }
GlanceMetrics    { heading: string; items: { label: string; value: number }[] }    // AnimatedCounter, no clock
ReproduceTabs    { heading: string; tabs: { label: string; code: string }[] }
LimitationsGrid  { heading: string; items: string[] }                              // one caveat per card
ContactCta       { heading: string; body: string; action: Link }
FindingsFooter   { name: string; links: Link[] }
```

Data per section: Hero, MethodSteps, ReproduceTabs, ContactCta, Footer, finding texts → `meta.json`; StatsStrip,
SystemsMeasured, GlanceMetrics, finding figures → `summary.json`; LimitationsGrid → caveats of the representative
bundle (or `summary.json.caveats` copied from `report.json.caveats`).

## 2. pulse-metrics → AppShell and `/results`

| # | File | Renders | Hardcoded content | Deps | Decision |
|---|---|---|---|---|---|
| 18 | `app/page.tsx` | page that holds `activeSection` state and the 3 columns | `Section` union of 10 ids | – | **REWRITE**: routes replace the state |
| 19 | `app/layout.tsx` | fonts (DM Sans, Source Serif 4, JetBrains Mono), metadata, Analytics | font set, title | `next/font/google`, `@vercel/analytics` | **DELETE** (tokens move in 25-1; Q4) |
| 20 | `app-sidebar.tsx` | 260 px sidebar: logo "Pulse" + **Live** pill, search box, "Quick Access", "Operations" menu with **badge counts (3, 8, 24)**, Settings, user "John Doe / SRE Lead", theme toggle | all of those | lucide, `Section` | **REUSE** as `shell/AppShell` sidebar: nav = Replay, Agent, Results, Method; `Link` + `usePathname` instead of `onSectionChange`; DELETE Live pill, search, Quick Access, badges, profile; theme toggle = our `ThemeToggle` |
| 21 | `main-content.tsx` | header (title/subtitle per section, Bell, Calendar, RefreshCw, "Report Incident", AlertCircle) + switch over the 10 views | titles/subtitles, button labels | lucide, `Button` | **REUSE** only the header frame as `shell/PageHeader`; DELETE every button and the switch |
| 22 | `right-panel.tsx` | right rail: System Status (99.98%, 142 ms), Recent Activity, On-Call Team (4 invented people) | all of it | lucide | **DELETE** (mapping) |
| 23 | `content/overview-content.tsx` | 4 KPI cards with +/- deltas, requests area chart, latency bar chart, active-incident list | KPIs 3 / 8 / 0.42% / 99.98% with deltas, 2 chart series, 3 incidents | recharts | **REUSE** KPI card + card shadow as `results/ResultsKpiCard` (no delta arrows); DELETE the two charts' data; the chart frame is reused by `FirstDenyChart` (row 26) |
| 24 | `content/incidents-content.tsx` | search + status filter, incident list, detail panel with **timeline** (Alert triggered, …) | 4 incidents, severity/status maps, timelines | state, `Input`, `Button` | **REUSE** list + detail + timeline as `results/ScenarioList`, `ScenarioDetail`, `ScenarioTimeline`; DELETE search, filters, severity map, assignee, "ExternalLink" |
| 25 | `content/performance-content.tsx` | 4 KPI with deltas, line chart, area chart, **Service Latencies table** | p50/p95/p99 per service, series | recharts | **REUSE** the table frame as `results/ResultsTable`; DELETE KPIs, series, services |
| 26 | (chart frames in overview/performance/errors) | area/line/bar charts | series | recharts | **REUSE** card frame + axis styling for `results/FirstDenyChart`. **Does not fit the mapping 1:1**: the template charts are area/line/bar; ours is a log-scale dot (scatter) plot, so only styling carries over (Q6) |
| 27 | `content/deployments-content.tsx` | deploy frequency chart, deploy list, 4 KPIs | deploys, branches, authors | recharts | **DELETE** (not in mapping; no equivalent in our data) |
| 28 | `content/errors-content.tsx` | error trend, funnel, top errors, 4 KPIs | invented errors | recharts | **DELETE** |
| 29 | `content/sla-content.tsx` | uptime history, SLA targets, services, outages | 99.98%, targets, outages | recharts | **DELETE** (uptime claims are banned by the new guard) |
| 30 | `content/oncall-content.tsx` | schedule, pages, response times, 4 KPIs | 4 people, pages | recharts | **DELETE** ("On-Call" banned) |
| 31 | `content/services-content.tsx` | 8 services with status cards | service names, versions | `Button` | **DELETE** |
| 32 | `content/postmortems-content.tsx` | postmortem list + 4 KPIs | 5 invented postmortems | `Button` | **DELETE** |
| 33 | `content/settings-content.tsx` | settings categories, integrations (PagerDuty, Slack, Datadog, GitHub, Jira) | all | `Button` | **DELETE** (third-party brands, no settings) |
| 34 | `lib/data.ts`, `lib/types.ts` | research-study mock data and types | studies, insights, participants | – | **DELETE** (unused leftovers of another template) |
| 35 | `lib/utils.ts`, `hooks/*`, `components/theme-provider.tsx`, `public/*` | `cn`, mobile hook, toast hook, v0 placeholder images and icons | placeholders | – | `cn` and theme-provider already exist; the rest **DELETE** |

### Props after stripping (Shell and Results)

```ts
interface NavItem { href: string; label: string; icon: LucideIcon }
AppShell        { nav: NavItem[]; children: ReactNode }       // hidden when ShellVisibility says presentation
PageHeader      { title: string; subtitle?: string }          // no actions slot
ResultsKpiCard  { scenario: string; caption: string;
                  rows: { system: SystemId; label: string; value: string; n: string }[] }
ScenarioList    { items: { id: string; title: string; n: string;
                  statuses: { system: SystemId; status: string }[] }[]; selectedId: string }   // links, no filters
ScenarioDetail  { title: string; description: string; n: string;
                  perSystem: { id: SystemId; label: string; median: string; min: string; max: string;
                               leakExposure: string; dnc: string }[];
                  bundles: { label: string; href: string; representative: boolean }[] }
ScenarioTimeline{ title: string; events: { at: string; label: string; system?: SystemId;
                  tone: "neutral" | "deny" | "ok" | "unscored" }[] }   // from the phase and mutation events of one bundle
FirstDenyChart  { points: { system: SystemId; replicate: number; value: number; censored: boolean }[];
                  cap: number; systems: { id: SystemId; label: string }[] }                  // log axis, every replicate a dot
ResultsTable    { rows: { scenario: string; system: string; median: string; min: string; max: string;
                          leakExposure: string; dnc: string; n: string }[] }
```

Data: `report.json` `cells[]` (`first_deny`, `leak_exposure`, `did_not_converge`, `rows[].first_deny_s`,
`replicates`) for KPI, chart, table and list; `index.json` for bundle links and the representative flag; one bundle's
`phase` and `mutation` events for the timeline. Formatting uses the existing `lib/replay/scale.ts` helpers.

## 3. beam-main → `/agent` flow view

| # | File | Renders | Hardcoded content | Deps | Decision |
|---|---|---|---|---|---|
| 36 | `components/animated-beam-demo.tsx` | 7 circles (Drive, Docs, Notion, OpenAI, Zapier, WhatsApp, Messenger) around one hub, 6 beams | **6 third-party brand SVGs** (`Icons.*`, ~300 lines), layout, curvature offsets | `AnimatedBeam`, refs | **REUSE the layout skeleton** (container, `Circle`, refs, beams) as `agent/FlowGraph`; DELETE all six brand SVGs |
| 37 | `components/magicui/animated-beam.tsx` | SVG path between two refs with animated gradient, ResizeObserver | `Math.random()` duration, default colors | **`motion/react`**, Tailwind-3 classes | **REUSE** as `agent/AnimatedBeam` with a **deterministic** duration prop and `tone` colors (leak = red, ok = green, denied = grey, idle = faint). Port to Tailwind 4; see Q3 on `motion` |
| 38 | `app/page.tsx`, `app/layout.tsx` | demo page, Geist fonts, Analytics | – | – | **DELETE** |
| 39 | `tailwind.config.ts`, `styles/globals.css` | Tailwind 3 config and tokens | theme extension for beam keyframes | Tailwind 3 | **DELETE** (the 25-1 tokens live in the Tailwind 4 CSS) |
| 40 | `components/ui/*` (57), `public/*`, hooks, theme-provider | shadcn Tailwind-3 variants, placeholders | – | Radix | **DELETE** |

```ts
type FlowNodeId = "asking" | "earlier" | "cache" | "memory" | "retrieval" | "guard" | "llm";
interface FlowNode { id: FlowNodeId; label: string; sublabel?: string; icon: LucideIcon }   // lucide icons, no logos
interface FlowBeam { from: FlowNodeId; to: FlowNodeId; tone: "ok" | "leak" | "denied" | "idle"; label?: string }
FlowGraph  { nodes: FlowNode[]; beams: FlowBeam[]; note?: string }                          // pure, no bundle knowledge
AnimatedBeam { containerRef; fromRef; toRef; tone; curvature?; reverse?; duration: number; delay?: number }
```

A small adapter `lib/agent/flow.ts` (new, not in `lib/replay/`) turns **one turn of an `agent_trial` bundle** into
`beams`: `path = cache_hit` lights asking → cache → asking; `memory_hit` asking → memory → LLM; `retrieval` asking →
retrieval (Setup A or B, from `trial.arm.id`) → LLM; `guard_decision = denied` adds a grey beam to the guard with the
`guard_reason` as `label`; `verdict = leak` makes the delivered beam red; an INVALID trial uses `idle`/grey only and the
existing grey banner. Retrieval label (Setup A vs B), arm mode and LLM ("stub") come from the bundle (`trial.arm`,
`run.llm`).

## 4. Does not fit the mapping (flags)

1. **Pulse sections the mapping does not mention** (deployments, errors, SLA/uptime, on-call, services, postmortems,
   settings) and `lib/data.ts`/`lib/types.ts`: all DELETE.
2. **`/method` has no source in the mapping.** AppShell nav lists "Method" but the mapping defines no Method page (Q2).
3. **The landing page vs the shell.** The mapping makes `/` the Findings page and the shell nav lists Replay, Agent,
   Results, Method. It does not say whether `/` sits inside the shell (Q1).
4. **Template charts are area/line/bar**, ours is a log-scale dot plot; only the chart card styling is reused (row 26).
5. **Beam is Tailwind 3 and needs `motion`**; the rest is Tailwind 4 (Q3).
6. **Pulse's `Section` state routing** (one page, `useState`) becomes real routes; none of its section switch is reused.
7. **Three different font sets** (Instrument, DM Sans, Geist) and token systems (optimus neutral/warm, pulse blue with
   `success/warning/info`, beam default): 25-1 must pick one (Q4). Pulse's `--success/--warning/--info` tokens are
   useful; our served/denied/no-answer/lockout/indexer tokens must stay.
8. **Existing code the stricter guard will meet:** a search for the proposed banned words in `components/`, `app/` and
   `lib/replay/` finds only the variable `live` in two loaders (`let live = true`), not the word "Live" as text. The guard
   should therefore be case-sensitive on "Live" or match whole words in rendered strings, not source identifiers (Q7).
9. **Public assets:** the v0 icon set (`apple-icon.png`, `icon.svg`, `icon-light/dark-32x32.png`) and all
   `placeholder*` files are template defaults; they are third-party-free but not ours. Replace or delete in 25-4.
10. **`report.json` is not in `public/` yet** and `summary.json` does not exist: backend work in 25-2/25-4 (below).

## 5. Backend work this plan implies

- `leaklab.replay export` also writes `summary.json` (schema `docs/schemas/summary.schema.json`): headline Entra-group
  medians per system with n, S1-spgroup and S1-direct medians for findings 01–02, S6 totals (naive vs guarded:
  leaks/trials, positive control served/trials), totals (runs, replicates, reads, documents, personas), caveats copied
  from `report.json`. Everything generated from `report.json` and the bundles; none typed.
- Copy `report.json` to `public/report.json` (it has 0 identifier hits; the redaction scan runs again on the copy) and
  validate it in the loader with a schema (new, small: only the fields the UI reads).
- `meta.json` is written by hand (non-measured text and URLs). Checked by a test that fails on any digit sequence that
  looks like a result, and on the new banned words.

## 6. Questions that need your decision before 25-1

- **Q1** Is `/` (Findings) outside the AppShell, with its own top bar, while Replay, Agent, Results and Method use the
  shell? (Recommended: yes. A shell around a landing page looks wrong, and presentation mode already hides it.)
- **Q2** What is "Method"? (Recommended: a `/method` page made of the "Method in three steps", "Reproduce it" and
  "Limitations" sections; `/` then keeps only hero, strip, three findings, systems measured, at a glance and the CTA.
  Alternative: Method is an anchor on `/`.)
- **Q3** `motion` (about 30 KB, a new dependency, one component) or our own beam in SVG (SMIL/CSS gradient animation, no
  dependency)? (Recommended: own SVG; the beam only needs a path, a moving gradient and ResizeObserver.)
- **Q4** Which fonts and token set wins? (Recommended: keep Geist, which is bundled locally so the build needs no
  network; take Pulse's radii/shadows and `success/warning/info`, optimus's grid background.)
- **Q5** Keep the particle sphere? (Recommended: no, keep it off the recording-critical pages; if kept, hero only,
  with reduced-motion support.)
- **Q6** The first-deny chart: recharts `ScatterChart` with a log Y axis (reuses the dependency already installed),
  or the SVG dot plot we already drew in matplotlib? (Recommended: recharts, styled with the Pulse card.)
- **Q7** Banned-word guard: whole-word, case-sensitive for "Live", case-insensitive for the compliance and pricing
  words, applied to rendered strings and `meta.json`, not to identifiers?
- **Q8** Who writes the `meta.json` copy (question, scope line, three finding titles and descriptions, CLI commands)?
  I can draft it from `docs/writeup/WRITEUP.template.md`; every claim in it is then yours to approve. I also need your
  LinkedIn URL and the write-up URL (not in the repo).
- **Q9** Until the overnight runs finish, every number on `/` and `/results` is n = 1. Should the pages show an
  explicit "n = 1, provisional" line? (Recommended: yes, generated from the data: it appears whenever a cell has fewer
  than three replicates, as the report's own caveat already does.)

## 7. Summary of decisions

REUSE (stripped to presentational components): landing nav, hero layout, stats-strip look, findings list, method steps,
systems list, glance counters, reproduce tabs, limitations grid, CTA box, footer; Pulse sidebar, header frame, KPI card,
incident list/detail/timeline, table frame, chart card frame; Beam layout skeleton and the beam primitive.
DELETE: integrations, testimonials, pricing, tetrahedron, wave, generic feature visuals, the Pulse right rail and seven
Pulse sections, Pulse `lib/data.ts` and `lib/types.ts`, all brand SVGs, Beam app/config/tokens, unused shadcn files,
placeholder assets, analytics. OPTIONAL: the particle sphere (Q5).
