"use client"

import { useRouter } from "next/navigation"
import { slugOf } from "@/lib/replay/load"
import type { Bundle, IndexFile, ScenarioEntry } from "@/lib/replay/types"

/** Picks a bundle from index.json: scenario, replicate, run id. */
export function BundlePicker({ index, current }: { index: IndexFile; current?: Bundle }) {
  const router = useRouter()
  const options = index.bundles.filter((b): b is ScenarioEntry => b.kind === "scenario_run")
  const selected = options.find(
    (b) =>
      current &&
      b.run_id === current.run.run_id &&
      b.scenario === current.scenario.id &&
      b.replicate === current.scenario.replicate,
  )
  return (
    <label className="flex items-center gap-2 text-sm">
      bundle
      <select
        aria-label="Bundle"
        className="max-w-xs rounded-md border border-border bg-card px-2 py-1"
        value={selected?.bundle ?? ""}
        onChange={(e) => router.push(`/replay/${slugOf(e.target.value)}`)}
      >
        {selected ? null : <option value="">choose a bundle</option>}
        {options.map((b) => (
          <option key={b.bundle} value={b.bundle}>
            {`${b.scenario} · replicate ${b.replicate} · ${b.run_id}${b.representative ? " · closest to the median" : ""}`}
          </option>
        ))}
      </select>
    </label>
  )
}
