/** Types of the replay data. The bundle types are GENERATED from schema.json (npm run gen:types). */
import type {
  IndexerRun,
  LeakLabReplayBundle,
  Mutation,
  Phase,
  PollerLag,
  Read,
} from "./types.generated"

export type { IndexerRun, Mutation, Phase, PollerLag, Read }
export type Bundle = LeakLabReplayBundle
export type SystemId = Read["system"]
export type Outcome = Read["outcome"]
export type Summary = Bundle["summary"][number]
export type SystemInfo = Bundle["run"]["systems"][number]

/** One line of public/replays/index.json, as written by the exporter. */
export interface IndexEntry {
  bundle: string
  run_id: string
  scenario: string
  replicate: number
  systems: SystemId[]
  summary: { system: SystemId; status: Summary["status"] }[]
}

export interface IndexFile {
  schema_version: string
  bundles: IndexEntry[]
}
