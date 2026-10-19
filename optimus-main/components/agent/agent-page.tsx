"use client"

/** Loads index.json (and one agent trial), validates both, and shows the trial or an error panel. */
import Link from "next/link"
import { useEffect, useState } from "react"
import { ErrorPanel } from "@/components/replay/error-panel"
import { Nav } from "@/components/replay/nav"
import { ThemeToggle } from "@/components/replay/theme-toggle"
import { loadAgentBundle, loadIndex, slugOf } from "@/lib/replay/load"
import type { AgentBundle, IndexFile } from "@/lib/replay/types"
import { AgentView, agentEntries } from "./agent-view"

type State =
  | { kind: "loading" }
  | { kind: "failed"; title: string; problems: string[]; total: number }
  | { kind: "index"; index: IndexFile }
  | { kind: "bundle"; index: IndexFile; bundle: AgentBundle }

export function AgentPage({ slug }: { slug?: string }) {
  const [state, setState] = useState<State>({ kind: "loading" })

  useEffect(() => {
    let live = true
    ;(async () => {
      const index = await loadIndex()
      if (!live) return
      if (!index.ok) return setState({ kind: "failed", title: "index.json could not be used", problems: index.problems, total: index.total })
      if (!slug) return setState({ kind: "index", index: index.value })
      const entry = agentEntries(index.value).find((e) => slugOf(e.bundle) === slug)
      if (!entry) {
        return setState({
          kind: "failed",
          title: "No such bundle",
          problems: [`${slug} is not listed in index.json as an agent trial`],
          total: 1,
        })
      }
      const bundle = await loadAgentBundle(entry.bundle)
      if (!live) return
      setState(
        bundle.ok
          ? { kind: "bundle", index: index.value, bundle: bundle.value }
          : { kind: "failed", title: `${entry.bundle} is not a valid agent trial bundle`, problems: bundle.problems, total: bundle.total },
      )
    })()
    return () => {
      live = false
    }
  }, [slug])

  if (state.kind === "loading") return <p className="p-6 text-sm text-muted-foreground">Loading trial data</p>
  if (state.kind === "failed") {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <ErrorPanel title={state.title} problems={state.problems} total={state.total} />
      </div>
    )
  }
  if (state.kind === "bundle") return <AgentView bundle={state.bundle} index={state.index} />
  const entries = agentEntries(state.index)
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Agent trials</h1>
        <div className="flex items-center gap-2">
          <Nav />
          <ThemeToggle />
        </div>
      </div>
      <ul className="space-y-2">
        {entries.map((e) => (
          <li key={e.bundle} className="rounded-md border border-border bg-card p-3 text-sm">
            <Link href={`/agent/${slugOf(e.bundle)}`} className="font-semibold underline-offset-4 hover:underline">
              {`${e.scenario} · ${e.arm} · trial ${e.trial}`}
            </Link>
            <span className="ml-2 font-mono text-xs text-muted-foreground">{`${e.verdict}${e.validity === "INVALID" ? " (invalid)" : ""}`}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
