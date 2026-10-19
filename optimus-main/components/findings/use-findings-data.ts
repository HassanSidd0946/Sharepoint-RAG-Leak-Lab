"use client"

import { useEffect, useState } from "react"
import { loadMeta, loadSummary } from "@/lib/findings/load"
import type { Meta, Summary } from "@/lib/findings/types"

export type FindingsData =
  | { state: "loading" }
  | { state: "failed"; problems: string[] }
  | { state: "ready"; meta: Meta; summary: Summary }

/** meta.json and summary.json, both validated; any problem with either is reported, never papered over. */
export function useFindingsData(): FindingsData {
  const [data, setData] = useState<FindingsData>({ state: "loading" })
  useEffect(() => {
    let live = true
    Promise.all([loadMeta(), loadSummary()]).then(([meta, summary]) => {
      if (!live) return
      if (meta.ok && summary.ok) return setData({ state: "ready", meta: meta.value, summary: summary.value })
      setData({ state: "failed", problems: [...(meta.ok ? [] : meta.problems), ...(summary.ok ? [] : summary.problems)] })
    })
    return () => {
      live = false
    }
  }, [])
  return data
}
