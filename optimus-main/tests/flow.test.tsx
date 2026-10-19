import { fireEvent, render, screen, within } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import { AgentView } from "@/components/agent/agent-view"
import { FlowGraph } from "@/components/agent/flow-graph"
import { flowFor } from "@/lib/agent/flow"
import type { AgentBundle, IndexFile } from "@/lib/replay/types"
import { checkAgentBundle, checkIndex } from "@/lib/replay/validate"
import guardedRaw from "./fixtures/agent.guarded.json"
import invalidRaw from "./fixtures/agent.invalid.json"
import leakRaw from "./fixtures/agent.leak.json"
import indexRaw from "./fixtures/index.v11.json"

function must<T>(r: { ok: true; value: T } | { ok: false; problems: string[] }): T {
  if (!r.ok) throw new Error(r.problems.join("; "))
  return r.value
}
const leak: AgentBundle = must(checkAgentBundle(leakRaw))
const guarded: AgentBundle = must(checkAgentBundle(guardedRaw))
const invalid: AgentBundle = must(checkAgentBundle(invalidRaw))
const index: IndexFile = must(checkIndex(indexRaw))

const beam = (flow: ReturnType<typeof flowFor>, from: string, to: string) =>
  flow.beams.find((b) => b.from === from && b.to === to)

/** The fixtures: turn 0 is the seed (retrieval), turn 1 the follow-up. */
describe("flowFor", () => {
  it("draws a leaking cache hit as a red path through the cache back to the asking user", () => {
    const f = flowFor(leak, 1)
    expect(beam(f, "asking", "cache")?.tone).toBe("leak")
    expect(beam(f, "cache", "asking")?.tone).toBe("leak")
    expect(beam(f, "earlier", "cache")?.tone).toBe("idle")
    expect(f.beams.some((b) => b.from === "retrieval")).toBe(false) // retrieval was not on the path
    expect(f.note).toMatch(/shared cache.*delivered to a user who may not read it/)
    expect(f.nodes.find((n) => n.id === "guard")).toBeUndefined() // naive arm: no guard
  })

  it("draws the guard's denial as grey with its reason, and the retrieval answer as green", () => {
    const f = flowFor(guarded, 1)
    const denied = beam(f, "cache", "guard")
    expect(denied?.tone).toBe("denied")
    expect(denied?.label).toBe("denied:no_matching_grant")
    expect(beam(f, "asking", "retrieval")?.tone).toBe("ok")
    expect(beam(f, "llm", "asking")?.tone).toBe("ok")
    expect(f.beams.some((b) => b.tone === "leak")).toBe(false)
    expect(f.nodes.find((n) => n.id === "guard")?.active).toBe(true)
    expect(f.note).toMatch(/guard denied/i)
  })

  it("draws a seed turn as a retrieval path with no earlier user", () => {
    const f = flowFor(leak, 0)
    expect(f.nodes.some((n) => n.id === "earlier")).toBe(false)
    expect(beam(f, "asking", "retrieval")?.tone).toBe("ok")
  })

  it("never draws an INVALID trial in red or green", () => {
    for (let i = 0; i < invalid.turns.length; i++) {
      const f = flowFor(invalid, i)
      expect(f.beams.every((b) => b.tone === "idle")).toBe(true)
      expect(f.note).toMatch(/INVALID/)
    }
  })

  it("names the retrieval system and the LLM from the bundle", () => {
    expect(flowFor(leak, 1).nodes.find((n) => n.id === "retrieval")?.label).toBe("Azure AI Search (Setup A)")
    expect(flowFor(guarded, 1).nodes.find((n) => n.id === "retrieval")?.label).toBe("Revocation layer (Setup B)")
    expect(flowFor(leak, 1).nodes.find((n) => n.id === "llm")?.label).toBe(`LLM (${leak.run.llm})`)
  })

  it("draws a memory hit through the memory and the LLM, with retrieval beside it", () => {
    const m = JSON.parse(JSON.stringify(leakRaw))
    m.turns[1].path = "memory_hit"
    const f = flowFor(must(checkAgentBundle(m)), 1)
    expect(beam(f, "asking", "memory")?.tone).toBe("leak")
    expect(beam(f, "memory", "llm")?.tone).toBe("leak")
    expect(beam(f, "asking", "retrieval")?.tone).toBe("ok")
    expect(beam(f, "llm", "asking")?.tone).toBe("leak")
  })
})

describe("FlowGraph", () => {
  it("renders every node and one beam per beam, with the data the tests and the page read", () => {
    const f = flowFor(leak, 1)
    render(<FlowGraph flow={f} />)
    for (const n of f.nodes) expect(screen.getByTestId(`node-${n.id}`)).toHaveAttribute("data-active", String(n.active))
    const beams = screen.getAllByTestId("beam")
    expect(beams).toHaveLength(f.beams.length)
    const red = beams.filter((b) => b.dataset.tone === "leak").map((b) => `${b.dataset.from}>${b.dataset.to}`)
    expect(red).toEqual(["asking>cache", "cache>asking"])
    expect(screen.getByTestId("flow-note")).toHaveTextContent(f.note)
  })

  it("draws nodes that are not on the path faint", () => {
    render(<FlowGraph flow={flowFor(leak, 1)} />)
    expect(screen.getByTestId("node-memory")).toHaveAttribute("data-active", "false")
    expect(screen.getByTestId("node-cache")).toHaveAttribute("data-active", "true")
  })
})

describe("the agent page's flow view", () => {
  it("starts on the leaking turn and follows the selected turn", () => {
    render(<AgentView bundle={leak} index={index} />)
    const graph = screen.getByTestId("flow-graph")
    expect(within(graph).getAllByTestId("beam").some((b) => b.dataset.tone === "leak")).toBe(true)
    expect(screen.getByTestId("turn-1")).toHaveAttribute("data-selected", "true")
    fireEvent.click(within(screen.getByTestId("turn-0")).getByTestId("show-path"))
    expect(screen.getByTestId("turn-0")).toHaveAttribute("data-selected", "true")
    expect(screen.getByTestId("turn-1")).toHaveAttribute("data-selected", "false")
    expect(within(graph).getAllByTestId("beam").some((b) => b.dataset.tone === "leak")).toBe(false)
  })

  it("shows the guard's denial for the guarded arm without any red beam", () => {
    render(<AgentView bundle={guarded} index={index} />)
    const beams = within(screen.getByTestId("flow-graph")).getAllByTestId("beam")
    expect(beams.some((b) => b.dataset.tone === "denied" && b.dataset.label === "denied:no_matching_grant")).toBe(true)
    expect(beams.some((b) => b.dataset.tone === "leak")).toBe(false)
  })

  it("keeps the grey banner for an INVALID trial and draws no coloured beam", () => {
    render(<AgentView bundle={invalid} index={index} />)
    expect(screen.getByTestId("invalid-banner")).toBeInTheDocument()
    const beams = within(screen.getByTestId("flow-graph")).getAllByTestId("beam")
    expect(beams.every((b) => b.dataset.tone === "idle")).toBe(true)
  })
})
