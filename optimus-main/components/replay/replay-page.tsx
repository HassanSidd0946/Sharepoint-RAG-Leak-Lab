"use client"

/** Loads index.json (and one bundle), validates both, and shows the replay or an error panel. */
import Link from "next/link"
import { useEffect, useState } from "react"
import { loadBundle, loadIndex, loadScript, slugOf } from "@/lib/replay/load"
import type { Bundle, IndexFile, ScenarioEntry, ScriptStep } from "@/lib/replay/types"
import type { Checked } from "@/lib/replay/validate"
import { BundlePicker } from "./bundle-picker"
import { ErrorPanel } from "./error-panel"
import { ReplayView } from "./replay-view"

type Problem = { title: string; problems: string[]; total: number }
type State =
  | { kind: "loading" }
  | ({ kind: "failed" } & Problem)
  | { kind: "index"; index: IndexFile }
  | { kind: "bundle"; index: IndexFile; bundle: Bundle; script: ScriptStep[] | null; scriptProblem: Problem | null }

function failure(title: string, c: Extract<Checked<unknown>, { ok: false }>): State {
  return { kind: "failed", title, problems: c.problems, total: c.total }
}

export function ReplayPage({ slug }: { slug?: string }) {
  const [state, setState] = useState<State>({ kind: "loading" })

  useEffect(() => {
    let live = true
    ;(async () => {
      const index = await loadIndex()
      if (!live) return
      if (!index.ok) return setState(failure("index.json could not be used", index))
      if (!slug) return setState({ kind: "index", index: index.value })
      const entry = index.value.bundles.find((b) => b.kind === "scenario_run" && slugOf(b.bundle) === slug)
      if (!entry) {
        return setState({
          kind: "failed",
          title: "No such bundle",
          problems: [`${slug} is not listed in index.json as a benchmark replicate`],
          total: 1,
        })
      }
      const bundle = await loadBundle(entry.bundle)
      if (!live) return
      if (!bundle.ok) return setState(failure(`${entry.bundle} is not a valid replay bundle`, bundle))
      const script = await loadScript()
      if (!live) return
      setState({
        kind: "bundle",
        index: index.value,
        bundle: bundle.value,
        script: script && script.ok ? script.value : null,
        scriptProblem:
          script && !script.ok
            ? { title: "playback.json is invalid: scripted playback is off", problems: script.problems, total: script.total }
            : null,
      })
    })()
    return () => {
      live = false
    }
  }, [slug])

  if (state.kind === "loading") return <p className="p-6 text-sm text-muted-foreground">Loading replay data</p>
  if (state.kind === "failed") {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <ErrorPanel title={state.title} problems={state.problems} total={state.total} />
      </div>
    )
  }
  if (state.kind === "bundle") {
    return (
      <>
        {state.scriptProblem ? (
          <div className="mx-auto max-w-3xl p-4">
            <ErrorPanel {...state.scriptProblem} />
          </div>
        ) : null}
        <ReplayView bundle={state.bundle} index={state.index} script={state.script} />
      </>
    )
  }
  const replicates = state.index.bundles.filter((b): b is ScenarioEntry => b.kind === "scenario_run")
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Replays</h1>
        <BundlePicker index={state.index} />
      </div>
      <ul className="space-y-2">
        {replicates.map((b) => (
          <li key={b.bundle} className="rounded-xl border border-border bg-card p-3 shadow-card">
            <Link href={`/replay/${slugOf(b.bundle)}`} className="font-semibold underline-offset-4 hover:underline">
              {`${b.scenario} · replicate ${b.replicate}`}
            </Link>
            {b.representative ? <span className="ml-2 text-xs text-muted-foreground">closest to the median</span> : null}
            <div className="font-mono text-xs text-muted-foreground">{b.run_id}</div>
            <div className="mt-1 text-xs">{b.summary.map((s) => `${s.system}: ${s.status}`).join(" · ")}</div>
          </li>
        ))}
      </ul>
    </div>
  )
}
