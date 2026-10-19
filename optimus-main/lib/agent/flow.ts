/**
 * Turns one turn of an agent_trial bundle into the nodes and beams of the flow graph. It only reads
 * what the bundle says (path, guard decision, verdict, validity); it decides nothing about leaks.
 *
 * The beam colour follows the DELIVERED content: red if the bundle's verdict for the turn is leak,
 * green if it is ok, grey if the guard denied the cache entry, faint if the trial is INVALID or the turn
 * has no verdict (nothing is claimed).
 */
import { Bot, Brain, Layers, Search, ShieldCheck, User, UserRound, type LucideIcon } from "lucide-react"
import type { AgentBundle, AgentTurn } from "@/lib/replay/types"

export type FlowNodeId = "earlier" | "asking" | "cache" | "memory" | "retrieval" | "guard" | "llm"
export type BeamTone = "ok" | "leak" | "denied" | "idle"

export interface FlowNode {
  id: FlowNodeId
  label: string
  sublabel?: string
  icon: LucideIcon
  /** Grid cell of the node (1-based). */
  col: number
  row: number
  /** On the path of this turn (the others are drawn faint). */
  active: boolean
}

export interface FlowBeam {
  id: string
  from: FlowNodeId
  to: FlowNodeId
  tone: BeamTone
  label?: string
}

export interface Flow {
  nodes: FlowNode[]
  beams: FlowBeam[]
  /** One line under the graph saying what the path shows. */
  note: string
}

const RETRIEVER: Record<AgentBundle["trial"]["arm"]["id"], string> = {
  agent_naive_a: "Azure AI Search (Setup A)",
  agent_naive_b: "Revocation layer (Setup B)",
  agent_guarded_b: "Revocation layer (Setup B)",
}

const PATH_NAME: Record<AgentTurn["path"], string> = {
  cache_hit: "answered from the shared cache",
  memory_hit: "answered with the shared memory",
  retrieval: "answered from retrieval",
}

function deliveredTone(bundle: AgentBundle, turn: AgentTurn): BeamTone {
  if (bundle.validity === "INVALID") return "idle"
  if (turn.verdict === "leak") return "leak"
  if (turn.verdict === "ok") return "ok"
  return "idle"
}

export function flowFor(bundle: AgentBundle, turnIndex: number): Flow {
  const turn = bundle.turns[turnIndex]
  const persona = (id: string) => bundle.run.personas.find((p) => p.id === id)?.display ?? id
  const seed = bundle.turns.find((t) => t.step === "seed")
  const tone = deliveredTone(bundle, turn)
  const guarded = bundle.trial.arm.mode === "guarded"

  const nodes: FlowNode[] = [
    { id: "asking", label: persona(turn.persona), sublabel: "asking", icon: User, col: 1, row: 3, active: false },
    { id: "cache", label: "Semantic cache", sublabel: "shared", icon: Layers, col: 2, row: 1, active: false },
    { id: "memory", label: "Memory", sublabel: "shared", icon: Brain, col: 2, row: 2, active: false },
    { id: "retrieval", label: RETRIEVER[bundle.trial.arm.id], sublabel: "retrieval", icon: Search, col: 2, row: 3, active: false },
    { id: "llm", label: `LLM (${bundle.run.llm})`, icon: Bot, col: 3, row: 2, active: false },
  ]
  if (turn.step === "followup" && seed) {
    nodes.push({ id: "earlier", label: persona(seed.persona), sublabel: "asked earlier", icon: UserRound, col: 1, row: 1, active: false })
  }
  if (guarded) nodes.push({ id: "guard", label: "Guard", sublabel: "re-checks every source", icon: ShieldCheck, col: 3, row: 1, active: false })

  const beams: FlowBeam[] = []
  const add = (from: FlowNodeId, to: FlowNodeId, t: BeamTone, label?: string) =>
    beams.push({ id: `${from}-${to}`, from, to, tone: t, label })
  const hasEarlier = nodes.some((n) => n.id === "earlier")
  const viaRetrieval = (t: BeamTone) => {
    add("asking", "retrieval", t)
    add("retrieval", "llm", t)
  }

  if (turn.guard_decision === "denied") {
    add("asking", "cache", "denied", "candidate found")
    add("cache", "guard", "denied", turn.guard_reason ?? "denied")
    viaRetrieval(tone)
    add("llm", "asking", tone)
  } else if (turn.path === "cache_hit") {
    if (hasEarlier) add("earlier", "cache", "idle", "stored earlier")
    add("asking", "cache", tone)
    if (turn.guard_decision === "allowed") add("cache", "guard", "ok", turn.guard_reason ?? "allowed")
    add("cache", "asking", tone)
  } else if (turn.path === "memory_hit") {
    if (hasEarlier) add("earlier", "memory", "idle", "stored earlier")
    add("asking", "memory", tone)
    add("memory", "llm", tone)
    viaRetrieval(bundle.validity === "INVALID" ? "idle" : "ok")
    add("llm", "asking", tone)
  } else {
    viaRetrieval(tone)
    add("llm", "asking", tone)
  }

  const note =
    bundle.validity === "INVALID"
      ? "INVALID trial: the situation it was designed for did not happen, so no path is claimed to be right or wrong."
      : turn.guard_decision === "denied"
        ? `The guard denied the cached or remembered content for this user (${turn.guard_reason ?? "denied"}); the answer came from retrieval.`
        : turn.verdict === "leak"
          ? `${PATH_NAME[turn.path]}; delivered to a user who may not read it.`
          : `${PATH_NAME[turn.path]}.`

  const used = new Set<FlowNodeId>(beams.flatMap((b) => [b.from, b.to]))
  return { nodes: nodes.map((n) => ({ ...n, active: used.has(n.id) })), beams, note }
}
