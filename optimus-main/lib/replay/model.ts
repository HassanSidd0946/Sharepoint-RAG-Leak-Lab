/**
 * Turns a validated bundle into what the view draws. It groups and orders; it never decides
 * whether a read was right: outcome / expected / verdict are the exporter's, passed through.
 */
import type { Bundle, IndexerRun, Mutation, Read, Summary, SystemId } from "./types"

export const LANE_ORDER: readonly SystemId[] = ["setup0", "setup_a_basic", "setup_b"]

export interface PairRow {
  persona: string
  canary: string
  docLabel: string
  reads: Read[]
}

export interface LaneModel {
  id: SystemId
  label: string
  configLabel: string
  flipped: PairRow[]
  control: PairRow[]
  indexerRuns: IndexerRun[]
  firstDenyRel: number | null
  lastServedRel: number | null
  capRel: number | null
  /** The summary says the system never served the flipped pair: nothing to revoke (never red or green). */
  neverServed: boolean
  summary: Summary | null
}

export interface Model {
  lanes: LaneModel[]
  mutation: Mutation | null
  domain: { min: number; max: number }
  otherPairs: number
}

const PAD_S = 1

export function buildModel(bundle: Bundle): Model {
  const reads = bundle.events.filter((e): e is Read => e.type === "read")
  const mutation = bundle.events.find((e): e is Mutation => e.type === "mutation") ?? null
  const phases = bundle.events.filter((e) => e.type === "phase")
  const runs = bundle.events.filter((e): e is IndexerRun => e.type === "indexer_run")
  const key = (persona: string, canary: string) => `${persona}\u0000${canary}`
  const pairs = bundle.scenario.tracked_pairs

  const lanes: LaneModel[] = []
  for (const id of LANE_ORDER) {
    const info = bundle.run.systems.find((s) => s.id === id)
    if (!info) continue
    const rows = (role: "flipped" | "control"): PairRow[] =>
      pairs
        .filter((p) => p.role === role)
        .map((p) => ({
          persona: p.persona,
          canary: p.canary,
          docLabel: p.doc_label,
          reads: reads
            .filter((r) => r.system === id && key(r.persona, r.canary) === key(p.persona, p.canary))
            .sort((a, b) => a.t - b.t),
        }))
    const summary = bundle.summary.find((s) => s.system === id) ?? null
    const phase = (name: string) => phases.find((p) => p.name === name && p.system === id)
    const cap = phase("cap_reached")
    lanes.push({
      id,
      label: info.label,
      configLabel: info.config_label,
      flipped: rows("flipped"),
      control: rows("control"),
      indexerRuns: runs.filter((r) => r.system === id),
      firstDenyRel: phase("first_deny")?.t_rel_s ?? null,
      lastServedRel: phase("last_served")?.t_rel_s ?? null,
      neverServed: summary?.status === "NOTHING_TO_REVOKE",
      capRel: cap ? cap.t_rel_s : summary?.status === "DID_NOT_CONVERGE" ? bundle.run.poll_cap_s : null,
      summary,
    })
  }

  const times = reads.map((r) => r.t_rel_s)
  const min = Math.min(0, ...times)
  const max = Math.max(0, ...times)
  return {
    lanes,
    mutation,
    domain: max > min ? { min, max } : { min: min - PAD_S, max: max + PAD_S },
    otherPairs: pairs.filter((p) => p.role === "other").length,
  }
}

export type ClockState = "waiting" | "running" | "stopped" | "cap" | "never_served"

export interface ClockView {
  state: ClockState
  elapsed: number
}

/** The clock of one lane at playback time `time` (seconds relative to the change). */
export function laneClock(lane: LaneModel, time: number): ClockView {
  if (lane.neverServed) return { state: "never_served", elapsed: 0 }
  if (time < 0) return { state: "waiting", elapsed: 0 }
  if (lane.firstDenyRel !== null && time >= lane.firstDenyRel) {
    return { state: "stopped", elapsed: lane.firstDenyRel }
  }
  if (lane.firstDenyRel === null && lane.capRel !== null && time >= lane.capRel) {
    return { state: "cap", elapsed: lane.capRel }
  }
  return { state: "running", elapsed: time }
}
