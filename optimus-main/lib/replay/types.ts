/**
 * Types of the replay data. The bundle types are GENERATED from schema.v1.1.json (npm run gen:types).
 * A v1.0 bundle is checked against schema.v1.0.json and then upgraded to the v1.1 shape (see
 * validate.ts), so the rest of the app only knows one shape.
 */
import type {
  AgentTrial,
  IndexerRun,
  Mutation,
  Phase,
  PollerLag,
  Read,
  ScenarioRun,
} from "./types.generated"

export type { AgentTrial, IndexerRun, Mutation, Phase, PollerLag, Read, ScenarioRun }
export type Bundle = ScenarioRun
export type AgentBundle = AgentTrial
export type AnyBundle = Bundle | AgentBundle
export type SystemId = Read["system"]
export type Outcome = Read["outcome"]
export type Summary = Bundle["summary"][number]
export type SystemInfo = Bundle["run"]["systems"][number]
export type AgentTurn = AgentTrial["turns"][number]

/** An index.json entry of a benchmark replicate (v1.0 entries have no kind, rank or representative). */
export interface ScenarioEntry {
  kind: "scenario_run"
  bundle: string
  run_id: string
  scenario: string
  replicate: number
  systems: SystemId[]
  summary: { system: SystemId; status: Summary["status"] }[]
  rank?: number | null
  representative?: boolean
}

/** An index.json entry of one S6 trial. */
export interface AgentEntry {
  kind: "agent_trial"
  bundle: string
  run_id: string
  scenario: AgentTrial["trial"]["scenario"]
  arm: AgentTrial["trial"]["arm"]["id"]
  trial: number
  validity: AgentTrial["validity"]
  verdict: AgentTrial["summary"]["verdict"]
}

export type IndexEntry = ScenarioEntry | AgentEntry

export interface IndexFile {
  schema_version: string
  ranking?: { metric: string }
  bundles: IndexEntry[]
}

/** One step of the scripted playback (public/replays/playback.json). */
export interface ScriptStep {
  until_t_rel_s: number | "end"
  speed: number
}
