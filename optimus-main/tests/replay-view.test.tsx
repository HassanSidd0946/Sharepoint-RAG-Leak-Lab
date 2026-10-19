import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ReplayPage } from "@/components/replay/replay-page"
import { ReplayView } from "@/components/replay/replay-view"
import type { Bundle, IndexFile } from "@/lib/replay/types"
import fixture from "./fixtures/bundle.json"
import fixtureIndex from "./fixtures/index.json"

const bundle = fixture as unknown as Bundle
const index = fixtureIndex as unknown as IndexFile
const at = (t: number) => render(<ReplayView bundle={bundle} index={index} startTime={t} />)
const lane = (id: string) => screen.getByTestId(`lane-${id}`)
const clock = (id: string) => screen.getByTestId(`clock-${id}`)
const reads = bundle.events.flatMap((e) => (e.type === "read" ? [e] : []))
const firstDeny = (id: string) => {
  const p = bundle.events.find((e) => e.type === "phase" && e.name === "first_deny" && e.system === id)
  return p ? p.t_rel_s : NaN
}

afterEach(() => vi.unstubAllGlobals())

describe("lanes", () => {
  it("shows the three lanes in order, each with its label and configuration caveat", () => {
    at(0)
    const ids = screen.getAllByTestId(/^lane-(setup0|setup_a_basic|setup_b)$/).map((e) => e.dataset.testid)
    expect(ids).toEqual(["lane-setup0", "lane-setup_a_basic", "lane-setup_b"])
    for (const s of bundle.run.systems) {
      expect(within(lane(s.id)).getByText(s.label)).toBeInTheDocument()
      expect(within(lane(s.id)).getByText(s.config_label)).toBeInTheDocument()
    }
  })

  it("draws one tick per read of the flipped and control pairs, coloured by outcome", () => {
    at(0)
    const ticks = within(lane("setup0")).getAllByTestId("tick")
    expect(ticks).toHaveLength(reads.filter((r) => r.system === "setup0").length)
    for (const t of ticks.filter((x) => x.dataset.outcome === "served")) expect(t.style.stroke).toBe("var(--served)")
    const denied = ticks.filter((x) => x.dataset.outcome === "denied")
    expect(denied.length).toBeGreaterThan(0)
    for (const t of denied) expect(t.style.stroke).toBe("var(--denied)")
  })

  it("draws error and not_indexed reads grey, never red", () => {
    at(0)
    const noAnswer = screen
      .getAllByTestId("tick")
      .filter((t) => t.dataset.outcome === "error" || t.dataset.outcome === "not_indexed")
    const expected = reads.filter((r) => r.outcome === "error" || r.outcome === "not_indexed").length
    expect(expected).toBeGreaterThan(0)
    expect(noAnswer).toHaveLength(expected)
    for (const t of noAnswer) {
      expect(t.style.stroke).toBe("var(--noanswer)")
      expect(t.dataset.verdict).toBe("unscored")
    }
    expect(screen.queryAllByTestId("verdict-mark").filter((m) => m.dataset.verdict === "unscored")).toHaveLength(0)
  })

  it("marks exactly the leak ticks the bundle names", () => {
    at(0)
    const leaks = reads.filter((r) => r.verdict === "leak").length
    expect(leaks).toBeGreaterThan(0)
    expect(screen.getAllByTestId("verdict-mark").filter((m) => m.dataset.verdict === "leak")).toHaveLength(leaks)
  })

  it("shows indexer runs only where the bundle has them", () => {
    at(0)
    expect(within(lane("setup_a_basic")).getAllByTestId("indexer-run")).toHaveLength(1)
    expect(within(lane("setup0")).queryAllByTestId("indexer-run")).toHaveLength(0)
    expect(within(lane("setup_b")).queryAllByTestId("indexer-run")).toHaveLength(0)
  })
})

describe("clocks", () => {
  it("wait before the change, run after it, and freeze red at each lane's first_deny", () => {
    const before = at(-1)
    expect(clock("setup0").dataset.state).toBe("waiting")
    before.unmount()

    const during = render(<ReplayView bundle={bundle} index={index} startTime={firstDeny("setup_a_basic") + 1} />)
    expect(clock("setup_b").dataset.state).toBe("stopped")
    expect(clock("setup_a_basic").dataset.state).toBe("stopped")
    expect(clock("setup0").dataset.state).toBe("running")
    const frozenB = clock("setup_b").textContent
    const frozenA = clock("setup_a_basic").textContent
    during.unmount()

    at(reads[reads.length - 1].t_rel_s)
    expect(clock("setup0").dataset.state).toBe("stopped")
    expect(clock("setup_b").textContent).toBe(frozenB)
    expect(clock("setup_a_basic").textContent).toBe(frozenA)
    expect(clock("setup_b").className).toContain("--denied")
  })

  it("says the lane did not stop within the cap when the summary says DID_NOT_CONVERGE", () => {
    const b = JSON.parse(JSON.stringify(bundle)) as Bundle
    b.events = b.events.filter((e) => !(e.type === "phase" && e.system === "setup0"))
    const s = b.summary.find((x) => x.system === "setup0")
    if (s) s.status = "DID_NOT_CONVERGE"
    render(<ReplayView bundle={b} index={index} startTime={b.run.poll_cap_s + 1} />)
    expect(clock("setup0").dataset.state).toBe("cap")
    expect(clock("setup0")).toHaveTextContent("did not stop within cap")
  })
})

describe("playback, summary, footer", () => {
  it("labels the playback speed from state", () => {
    at(0)
    expect(screen.getByTestId("speed-label")).toHaveTextContent("Real run, played back at 1x")
    fireEvent.click(screen.getByRole("button", { name: "60x" }))
    expect(screen.getByTestId("speed-label")).toHaveTextContent("Real run, played back at 60x")
  })

  it("hides the controls but keeps the speed label in presentation mode", () => {
    at(0)
    fireEvent.click(screen.getByRole("button", { name: /presentation mode/i }))
    expect(screen.queryByTestId("controls")).toBeNull()
    expect(screen.getByTestId("speed-label")).toBeInTheDocument()
  })

  it("renders the summary, mutation text and provenance from the bundle", () => {
    at(0)
    for (const s of bundle.summary) expect(screen.getByTestId(`status-${s.system}`)).toHaveTextContent(s.status)
    const footer = screen.getByTestId("footer")
    expect(footer).toHaveTextContent(bundle.run.run_id)
    expect(footer).toHaveTextContent(bundle.run.generation)
    expect(footer).toHaveTextContent(bundle.provenance.git_commit)
    expect(footer).toHaveTextContent(/One test tenant/)
    expect(footer).toHaveTextContent(/Tenant admin out of scope/)
    const mutation = bundle.events.find((e) => e.type === "mutation")
    expect(screen.getByTestId("mutation-text")).toHaveTextContent(mutation && mutation.type === "mutation" ? mutation.summary : "")
  })
})

describe("loading", () => {
  const serve = (files: Record<string, unknown>) =>
    vi.stubGlobal("fetch", async (url: string) => {
      const name = String(url).replace("/replays/", "")
      return name in files
        ? { ok: true, status: 200, json: async () => files[name] }
        : { ok: false, status: 404, json: async () => ({}) }
    })

  it("shows the error panel, not a replay, for a bundle that fails the schema", async () => {
    const broken = JSON.parse(JSON.stringify(bundle))
    delete broken.summary
    serve({ "index.json": index, "fixture.json": broken })
    render(<ReplayPage slug="fixture" />)
    const panel = await screen.findByTestId("error-panel")
    expect(panel).toHaveTextContent(/not a valid replay bundle/)
    expect(screen.queryByTestId("replay")).toBeNull()
  })

  it("renders a valid bundle from the files and refuses a slug that is not in index.json", async () => {
    serve({ "index.json": index, "fixture.json": bundle })
    const ok = render(<ReplayPage slug="fixture" />)
    await waitFor(() => expect(screen.getByTestId("replay")).toBeInTheDocument())
    ok.unmount()
    render(<ReplayPage slug="nope" />)
    expect(await screen.findByTestId("error-panel")).toHaveTextContent(/not listed in index.json/)
  })
})
