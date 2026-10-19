"use client"

/**
 * One S6 trial: two chat panes (the user who asks now on the left, the earlier user on the right),
 * every turn with its path, similarity, sources, delivered canaries and guard decision, and the
 * trial summary. Verdicts are the bundle's; an INVALID trial gets a grey banner and is never "ok".
 */
import { useRouter } from "next/navigation"
import { slugOf } from "@/lib/replay/load"
import type { AgentBundle, AgentEntry, IndexFile } from "@/lib/replay/types"
import { cn } from "@/lib/utils"
import { ReplayFooter } from "@/components/replay/footer"
import { TurnCard } from "./turn-card"

const SCENARIO_ORDER: AgentEntry["scenario"][] = ["S6a", "S6b", "S6c", "positive_control"]
const ARM_ORDER: AgentEntry["arm"][] = ["agent_naive_a", "agent_naive_b", "agent_guarded_b"]
const ARM_PICKER: Record<AgentEntry["arm"], string> = {
  agent_naive_a: "naive A",
  agent_naive_b: "naive B",
  agent_guarded_b: "guarded B",
}
const SCENARIO_PICKER: Record<AgentEntry["scenario"], string> = {
  S6a: "S6a",
  S6b: "S6b",
  S6c: "S6c",
  positive_control: "positive control",
}
const pill = "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium"

const VERDICT_TONE: Record<AgentBundle["summary"]["verdict"], string> = {
  LEAK: "border-[color:var(--denied)] bg-[color:var(--denied)] text-white",
  OK: "border-[color:var(--served)]",
  SERVED: "border-[color:var(--served)]",
  OVERBLOCK: "border-[color:var(--lockout)]",
  INVALID: "border-border bg-muted text-muted-foreground",
}

export function agentEntries(index: IndexFile): AgentEntry[] {
  return index.bundles.filter((b): b is AgentEntry => b.kind === "agent_trial")
}

function Pickers({ bundle, index }: { bundle: AgentBundle; index: IndexFile }) {
  const router = useRouter()
  const all = agentEntries(index)
  const t = bundle.trial
  const go = (e: AgentEntry | undefined) => e && router.push(`/agent/${slugOf(e.bundle)}`)
  const find = (scenario: string, arm: string, trial: number) =>
    all.find((e) => e.scenario === scenario && e.arm === arm && e.trial === trial)
  const select = "rounded-md border border-border bg-card px-2 py-1 text-sm"
  return (
    <div className="flex flex-wrap items-center gap-3" data-testid="agent-pickers">
      <label className="flex items-center gap-2 text-sm">
        scenario
        <select
          aria-label="Scenario"
          className={select}
          value={t.scenario}
          onChange={(e) =>
            go(
              find(e.target.value, t.arm.id, t.trial) ??
                all.find((x) => x.scenario === e.target.value && x.arm === t.arm.id) ??
                all.find((x) => x.scenario === e.target.value),
            )
          }
        >
          {SCENARIO_ORDER.filter((s) => all.some((e) => e.scenario === s)).map((s) => (
            <option key={s} value={s}>
              {SCENARIO_PICKER[s]}
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-2 text-sm">
        arm
        <select
          aria-label="Arm"
          className={select}
          value={t.arm.id}
          onChange={(e) =>
            go(find(t.scenario, e.target.value, t.trial) ?? all.find((x) => x.scenario === t.scenario && x.arm === e.target.value))
          }
        >
          {ARM_ORDER.filter((a) => all.some((e) => e.scenario === t.scenario && e.arm === a)).map((a) => (
            <option key={a} value={a}>
              {ARM_PICKER[a]}
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-2 text-sm">
        trial
        <select
          aria-label="Trial"
          className={select}
          value={t.trial}
          onChange={(e) => go(find(t.scenario, t.arm.id, Number(e.target.value)))}
        >
          {all
            .filter((e) => e.scenario === t.scenario && e.arm === t.arm.id)
            .map((e) => (
              <option key={e.trial} value={e.trial}>
                {`${e.trial} of ${t.n}${e.validity === "INVALID" ? " · invalid" : ""}`}
              </option>
            ))}
        </select>
      </label>
    </div>
  )
}

export function AgentView({ bundle, index }: { bundle: AgentBundle; index: IndexFile }) {
  const invalid = bundle.validity === "INVALID"
  const persona = (id: string) => bundle.run.personas.find((p) => p.id === id)?.display ?? id
  const turns = bundle.turns.map((turn, i) => ({ turn, i }))
  const asking = turns.filter(({ turn }) => turn.step === "followup")
  const earlier = turns.filter(({ turn }) => turn.step === "seed")
  const s = bundle.summary
  return (
    <div data-testid="agent-view" className="mx-auto flex max-w-[1300px] flex-col gap-4 p-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold">{`${bundle.trial.scenario} · ${bundle.trial.arm.label} · trial ${bundle.trial.trial}`}</h1>
          <p className="text-sm text-muted-foreground">{`${bundle.trial.kind.replaceAll("_", " ")} · asking about ${bundle.trial.question_source}`}</p>
        </div>
      </header>
      <Pickers bundle={bundle} index={index} />

      {invalid ? (
        <div
          role="status"
          data-testid="invalid-banner"
          className="rounded-md border border-border bg-muted px-4 py-3 text-sm text-muted-foreground"
        >
          <strong className="text-foreground">INVALID</strong>
          {` — the situation this trial was designed for did not happen (${s.reason}). It says nothing about the system and is never counted as OK.`}
        </div>
      ) : null}

      {bundle.mutations.map((m) => (
        <p key={m.t} data-testid="mutation" className="text-sm">
          <span className="text-muted-foreground">change confirmed: </span>
          {m.summary}
        </p>
      ))}

      <div className="grid gap-4 md:grid-cols-2">
        <section aria-label="asking user" data-testid="pane-asking" className="space-y-3">
          <h2 className="text-sm font-semibold text-muted-foreground">Asking now</h2>
          {asking.map(({ turn, i }) => (
            <TurnCard key={i} turn={turn} index={i} persona={persona(turn.persona)} invalid={invalid} />
          ))}
        </section>
        <section aria-label="earlier user" data-testid="pane-earlier" className="space-y-3">
          <h2 className="text-sm font-semibold text-muted-foreground">Asked earlier</h2>
          {earlier.map(({ turn, i }) => (
            <TurnCard key={i} turn={turn} index={i} persona={persona(turn.persona)} invalid={invalid} />
          ))}
        </section>
      </div>

      <section data-testid="agent-summary" className="rounded-xl border border-border bg-card p-3 text-sm shadow-card">
        <div className="flex flex-wrap items-center gap-3">
          <span data-testid="trial-verdict" data-verdict={s.verdict} className={cn(pill, VERDICT_TONE[s.verdict])}>
            {s.verdict}
          </span>
          <span className="text-muted-foreground">{s.reason}</span>
        </div>
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 sm:grid-cols-[auto_1fr_auto_1fr]">
          <dt className="text-muted-foreground">cache status</dt>
          <dd className="font-mono">{s.cache_status}</dd>
          <dt className="text-muted-foreground">similarity</dt>
          <dd className="font-mono">{s.similarity === null ? "—" : s.similarity.toFixed(2)}</dd>
          <dt className="text-muted-foreground">canaries delivered</dt>
          <dd className="font-mono">{s.delivered}</dd>
          <dt className="text-muted-foreground">leaked</dt>
          <dd className="font-mono">{s.leaks}</dd>
          <dt className="text-muted-foreground">guard denials</dt>
          <dd className="font-mono">{s.guard_denials}</dd>
          <dt
            className="text-muted-foreground"
            title="Canaries sitting in the cache or memory that some persona may not read. What a leak could draw on; not a leak count."
          >
            exposure surface
          </dt>
          <dd className="font-mono">{`${s.exposure_unsafe} of ${s.exposure_stored} stored`}</dd>
        </dl>
      </section>

      <ReplayFooter bundle={bundle} />
    </div>
  )
}
