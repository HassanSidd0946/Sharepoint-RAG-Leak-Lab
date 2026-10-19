import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { AgentPage } from "@/components/agent/agent-page"
import { AgentView } from "@/components/agent/agent-view"
import { ReplayPage } from "@/components/replay/replay-page"
import { ReplayView } from "@/components/replay/replay-view"
import type { AgentBundle, Bundle, IndexFile, ScriptStep } from "@/lib/replay/types"
import { checkAgentBundle, checkAnyBundle, checkBundle, checkIndex } from "@/lib/replay/validate"
import agentGuardedRaw from "./fixtures/agent.guarded.json"
import agentInvalidRaw from "./fixtures/agent.invalid.json"
import agentLeakRaw from "./fixtures/agent.leak.json"
import bundleRaw from "./fixtures/bundle.v11.json"
import indexRaw from "./fixtures/index.v11.json"

function must<T>(r: { ok: true; value: T } | { ok: false; problems: string[] }): T {
  if (!r.ok) throw new Error(`fixture is invalid: ${r.problems.join("; ")}`)
  return r.value
}

const bundle: Bundle = must(checkBundle(bundleRaw))
const index: IndexFile = must(checkIndex(indexRaw))
const leak: AgentBundle = must(checkAgentBundle(agentLeakRaw))
const guarded: AgentBundle = must(checkAgentBundle(agentGuardedRaw))
const invalid: AgentBundle = must(checkAgentBundle(agentInvalidRaw))

const at = (t: number, props: Partial<Parameters<typeof ReplayView>[0]> = {}) =>
  render(<ReplayView bundle={bundle} index={index} startTime={t} {...props} />)
const lane = (id: string) => screen.getByTestId(`lane-${id}`)
const clock = (id: string) => screen.getByTestId(`clock-${id}`)
const phase = (name: string, system: string) =>
  bundle.events.flatMap((e) => (e.type === "phase" && e.name === name && e.system === system ? [e] : []))[0]

afterEach(() => vi.unstubAllGlobals())

describe("schema v1.1 validation", () => {
  it("accepts the v1.1 fixtures", () => {
    expect(checkAnyBundle(bundleRaw).ok).toBe(true)
    for (const raw of [agentLeakRaw, agentGuardedRaw, agentInvalidRaw]) expect(checkAnyBundle(raw).ok).toBe(true)
  })

  it("needs caveats and a known kind, and refuses a status the schema does not have", () => {
    const noCaveats = JSON.parse(JSON.stringify(bundleRaw))
    delete noCaveats.caveats
    expect(checkBundle(noCaveats).ok).toBe(false)
    const badStatus = JSON.parse(JSON.stringify(bundleRaw))
    badStatus.summary[0].status = "NO_DATA"
    expect(checkBundle(badStatus).ok).toBe(false)
    const badKind = JSON.parse(JSON.stringify(bundleRaw))
    badKind.kind = "mystery"
    expect(checkAnyBundle(badKind).ok).toBe(false)
  })

  it("will not take an agent trial as a benchmark replicate or the other way round", () => {
    expect(checkBundle(agentLeakRaw).ok).toBe(false)
    expect(checkAgentBundle(bundleRaw).ok).toBe(false)
  })
})

describe("NOTHING_TO_REVOKE", () => {
  it("shows the lane in neutral grey with its caveat and the same words on the clock", () => {
    at(0)
    const clockEl = clock("setup_b")
    expect(clockEl.dataset.state).toBe("never_served")
    expect(clockEl).toHaveTextContent("Never served — nothing to revoke")
    expect(clockEl.className).not.toContain("--denied")
    expect(screen.getByTestId("never-served-setup_b")).toHaveTextContent("Never served — nothing to revoke")
    const config = bundle.run.systems.find((s) => s.id === "setup_b")?.config_label ?? ""
    expect(within(lane("setup_b")).getByText(config)).toBeInTheDocument()
  })

  it("draws none of the lane's ticks red or green, and puts no first-denial marker on it", () => {
    at(0)
    const ticks = within(lane("setup_b")).getAllByTestId("tick")
    expect(ticks.length).toBeGreaterThan(0)
    for (const t of ticks) expect(t.style.stroke).toBe("var(--noanswer)")
    expect(within(lane("setup_b")).queryByTestId("marker-first-deny")).toBeNull()
    expect(screen.getByTestId("status-setup_b")).toHaveTextContent("NOTHING_TO_REVOKE")
    expect(screen.getByTestId("first-deny-setup_b")).toHaveTextContent("—")
  })

  it("leaves the other lanes as they are", () => {
    at(0)
    expect(within(lane("setup0")).queryByTestId("never-served-setup0")).toBeNull()
    const served = within(lane("setup0")).getAllByTestId("tick").filter((t) => t.dataset.outcome === "served")
    expect(served.length).toBeGreaterThan(0)
    for (const t of served) expect(t.style.stroke).toBe("var(--served)")
  })
})

describe("last_served", () => {
  it("draws a last-served marker next to the first-denial marker, only where the bundle has the event", () => {
    at(0)
    expect(within(lane("setup0")).getByTestId("marker-last-served")).toBeInTheDocument()
    expect(within(lane("setup0")).getByTestId("marker-first-deny")).toBeInTheDocument()
    expect(within(lane("setup_b")).queryByTestId("marker-last-served")).toBeNull()
  })

  it("says when the file was last served once playback has passed that moment", () => {
    const rel = phase("last_served", "setup0").t_rel_s
    const before = at(rel - 1)
    expect(screen.queryByTestId("last-served-setup0")).toBeNull()
    before.unmount()
    at(rel + 1)
    expect(screen.getByTestId("last-served-setup0")).toHaveTextContent("last served at")
  })

  it("shows first denial and leak exposure in the summary with a tooltip about flapping", () => {
    at(0)
    const exposure = screen.getByTestId("leak-exposure-setup0")
    expect(exposure.title).toMatch(/flapping/)
    expect(screen.getByTestId("first-deny-setup0")).toBeInTheDocument()
  })
})

describe("caveats", () => {
  it("are listed from the bundle and nowhere else", () => {
    at(0)
    const list = screen.getByTestId("caveats")
    for (const c of bundle.caveats) expect(within(list).getByText(c)).toBeInTheDocument()
    expect(screen.queryByText(/One test tenant/)).toBeNull()
  })
})

describe("representative replicate and scripted playback", () => {
  const script: ScriptStep[] = [
    { until_t_rel_s: 5, speed: 1 },
    { until_t_rel_s: 30, speed: 60 },
    { until_t_rel_s: "end", speed: 300 },
  ]
  const label = () => screen.getByTestId("speed-label")

  it("captions the replicate the export ranked closest to the median", () => {
    at(0)
    expect(screen.getByTestId("representative-caption")).toHaveTextContent("Replicate closest to the median")
  })

  it("shows the current script speed in the label: 1x, then 60x, then 300x", () => {
    const expected: [number, string][] = [
      [0, "1x"],
      [4.9, "1x"],
      [5, "60x"],
      [29.9, "60x"],
      [30, "300x"],
      [45, "300x"],
    ]
    for (const [t, speed] of expected) {
      const view = at(t, { script, initialPresentation: true })
      expect(label(), `at t=${t}`).toHaveTextContent(`Real run, played back at ${speed}`)
      view.unmount()
    }
  })

  it("does not use the script outside presentation mode", () => {
    at(45, { script })
    expect(label()).toHaveTextContent("played back at 1x")
    expect(screen.getByTestId("controls")).toBeInTheDocument()
  })

  it("goes back to the script start, paused, when presentation mode is entered", () => {
    at(20, { script })
    fireEvent.click(screen.getByRole("button", { name: /presentation mode/i }))
    expect(screen.queryByTestId("controls")).toBeNull()
    expect(label()).toHaveTextContent("played back at 1x")
    expect(clock("setup0").dataset.state).toBe("waiting") // t_rel = start of the script, before the change
  })
})

describe("loading with a script", () => {
  const serve = (files: Record<string, unknown>) =>
    vi.stubGlobal("fetch", async (url: string) => {
      const name = String(url).replace("/replays/", "")
      return name in files
        ? { ok: true, status: 200, json: async () => files[name] }
        : { ok: false, status: 404, json: async () => ({}) }
    })

  it("warns, and keeps the replay, when playback.json is invalid", async () => {
    serve({ "index.json": indexRaw, "fixture-v11.json": bundleRaw, "playback.json": [{ until_t_rel_s: 5, speed: 1 }] })
    render(<ReplayPage slug="fixture-v11" />)
    await waitFor(() => expect(screen.getByTestId("replay")).toBeInTheDocument())
    expect(screen.getByTestId("error-panel")).toHaveTextContent(/playback.json is invalid/)
  })

  it("plays without a script when there is no playback.json", async () => {
    serve({ "index.json": indexRaw, "fixture-v11.json": bundleRaw })
    render(<ReplayPage slug="fixture-v11" />)
    await waitFor(() => expect(screen.getByTestId("replay")).toBeInTheDocument())
    expect(screen.queryByTestId("error-panel")).toBeNull()
  })
})

describe("agent trials", () => {
  const renderAgent = (b: AgentBundle) => render(<AgentView bundle={b} index={index} />)

  it("puts the asking user on the left and the earlier user on the right", () => {
    renderAgent(leak)
    const asking = screen.getByTestId("pane-asking")
    const earlier = screen.getByTestId("pane-earlier")
    expect(within(asking).getAllByTestId(/^turn-\d+$/)).toHaveLength(1)
    expect(within(earlier).getAllByTestId(/^turn-\d+$/)).toHaveLength(1)
    expect(asking).toHaveTextContent("Ada Fixture")
    expect(earlier).toHaveTextContent("Bo Fixture")
  })

  it("shows path, similarity, sources and the delivered canary in red when it leaked", () => {
    renderAgent(leak)
    const turn = within(screen.getByTestId("pane-asking")).getByTestId("turn-1")
    expect(within(turn).getByTestId("path-badge")).toHaveTextContent("cache hit")
    expect(within(turn).getByTestId("similarity")).toHaveTextContent("0.83")
    expect(turn).toHaveTextContent("Finance/doc-01.txt")
    const chip = within(turn).getByTestId("canary")
    expect(chip.dataset.verdict).toBe("leak")
    expect(chip.className).toContain("--denied")
    expect(within(turn).getByTestId("guard-decision")).toHaveTextContent("no guard")
    expect(screen.getByTestId("trial-verdict")).toHaveTextContent("LEAK")
  })

  it("shows the guard's decision and reason, and no red canary, for the guarded arm", () => {
    renderAgent(guarded)
    const turn = within(screen.getByTestId("pane-asking")).getByTestId("turn-1")
    expect(within(turn).getByTestId("guard-decision")).toHaveTextContent("guard denied")
    expect(within(turn).getByTestId("guard-reason")).toHaveTextContent("denied:no_matching_grant")
    for (const chip of within(turn).getAllByTestId("canary")) expect(chip.dataset.verdict).toBe("ok")
    expect(screen.queryByTestId("invalid-banner")).toBeNull()
  })

  it("marks an INVALID trial with a grey banner and never shows it as ok", () => {
    renderAgent(invalid)
    expect(screen.getByTestId("invalid-banner")).toHaveTextContent("INVALID")
    expect(screen.getByTestId("invalid-banner")).toHaveTextContent("no_cache_candidate")
    const verdict = screen.getByTestId("trial-verdict")
    expect(verdict).toHaveTextContent("INVALID")
    expect(verdict.className).not.toContain("--served")
    for (const v of screen.getAllByTestId("turn-verdict")) {
      expect(v).toHaveTextContent("no verdict")
      expect(v.textContent).not.toMatch(/^ok$/)
    }
  })

  it("offers the arms and trials the index lists", () => {
    renderAgent(leak)
    const arm = screen.getByRole("combobox", { name: "Arm" })
    expect(within(arm).getAllByRole("option").map((o) => o.textContent)).toEqual(["naive A", "guarded B"])
    const trial = screen.getByRole("combobox", { name: "Trial" })
    expect(within(trial).getAllByRole("option").map((o) => o.textContent)).toEqual(["1 of 3", "2 of 3 · invalid"])
  })

  const serve = (files: Record<string, unknown>) =>
    vi.stubGlobal("fetch", async (url: string) => {
      const name = String(url).replace("/replays/", "")
      return name in files
        ? { ok: true, status: 200, json: async () => files[name] }
        : { ok: false, status: 404, json: async () => ({}) }
    })

  it("shows the error panel for an agent bundle that fails the schema", async () => {
    const broken = JSON.parse(JSON.stringify(agentLeakRaw))
    delete broken.turns
    serve({ "index.json": indexRaw, "agent-leak.json": broken })
    render(<AgentPage slug="agent-leak" />)
    expect(await screen.findByTestId("error-panel")).toHaveTextContent(/not a valid agent trial bundle/)
    expect(screen.queryByTestId("agent-view")).toBeNull()
  })

  it("loads and shows a valid trial", async () => {
    serve({ "index.json": indexRaw, "agent-leak.json": agentLeakRaw })
    render(<AgentPage slug="agent-leak" />)
    await waitFor(() => expect(screen.getByTestId("agent-view")).toBeInTheDocument())
  })
})
