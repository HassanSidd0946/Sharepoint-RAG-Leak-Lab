import { render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ReplayPage } from "@/components/replay/replay-page"
import { ReplayView } from "@/components/replay/replay-view"
import { AppShell } from "@/components/shell/app-shell"
import { PageHeader } from "@/components/shell/page-header"
import { ShellVisibilityProvider } from "@/components/shell/shell-visibility"
import { NAV } from "@/lib/ui/nav"
import type { Bundle, IndexFile } from "@/lib/replay/types"
import { checkBundle, checkIndex } from "@/lib/replay/validate"
import bundleRaw from "./fixtures/bundle.v11.json"
import indexRaw from "./fixtures/index.v11.json"

function must<T>(r: { ok: true; value: T } | { ok: false; problems: string[] }): T {
  if (!r.ok) throw new Error(r.problems.join("; "))
  return r.value
}
const bundle: Bundle = must(checkBundle(bundleRaw))
const index: IndexFile = must(checkIndex(indexRaw))

afterEach(() => vi.unstubAllGlobals())

describe("AppShell", () => {
  it("has exactly the four views in the navigation, and nothing else from the Pulse template", () => {
    render(
      <AppShell brand="Test" nav={NAV}>
        <p>page</p>
      </AppShell>,
    )
    const nav = screen.getByRole("navigation", { name: "Main" })
    expect(within(nav).getAllByRole("link").map((l) => l.textContent)).toEqual(["Replay", "Agent", "Results", "Method"])
    expect(screen.queryByPlaceholderText(/search/i)).toBeNull()
    expect(screen.queryByText(/report incident|on-call|notifications/i)).toBeNull()
  })

  it("marks the current page", () => {
    render(
      <AppShell brand="Test" nav={NAV}>
        <p>page</p>
      </AppShell>,
    )
    const nav = screen.getByRole("navigation", { name: "Main" })
    expect(within(nav).getByRole("link", { name: "Replay" })).toHaveAttribute("aria-current", "page")
    expect(within(nav).getByRole("link", { name: "Agent" })).not.toHaveAttribute("aria-current")
  })

  it("holds no data: it renders its children and the brand it is given", () => {
    render(
      <AppShell brand="A brand" nav={NAV}>
        <p>page body</p>
      </AppShell>,
    )
    expect(screen.getAllByText("A brand").length).toBeGreaterThan(0)
    expect(screen.getByText("page body")).toBeInTheDocument()
  })
})

describe("presentation mode hides the shell completely", () => {
  const page = () => (
    <ShellVisibilityProvider>
      <AppShell brand="Test" nav={NAV}>
        <ReplayView bundle={bundle} index={index} startTime={0} />
      </AppShell>
    </ShellVisibilityProvider>
  )

  it("shows the shell on the normal page", () => {
    render(page())
    expect(screen.getByTestId("app-shell")).toBeInTheDocument()
    expect(screen.getByTestId("replay")).toBeInTheDocument()
  })

  it("hides the shell's chrome, and keeps the very same replay mounted, when presentation mode starts", async () => {
    render(page())
    screen.getByRole("button", { name: /presentation mode/i }).click()
    await waitFor(() => expect(screen.getByTestId("app-shell")).toHaveAttribute("data-hidden", "true"))
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull() // hidden elements are not in the accessibility tree
    expect(screen.getByTestId("replay")).toHaveAttribute("data-presentation", "true")
    expect(screen.getByTestId("speed-label")).toBeInTheDocument()
  })

  it("brings the shell back when presentation mode ends", async () => {
    render(page())
    screen.getByRole("button", { name: /presentation mode/i }).click()
    await waitFor(() => expect(screen.getByTestId("app-shell")).toHaveAttribute("data-hidden", "true"))
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))
    await waitFor(() => expect(screen.getByTestId("app-shell")).toHaveAttribute("data-hidden", "false"))
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument()
  })
})

describe("presentation mode does not reload the page", () => {
  it("keeps the loaded replay: no second fetch, the replay stays on screen in presentation mode", async () => {
    const calls: string[] = []
    vi.stubGlobal("fetch", async (url: string) => {
      calls.push(String(url))
      const body = String(url).endsWith("index.json") ? indexRaw : bundleRaw
      return String(url).endsWith("playback.json")
        ? { ok: false, status: 404, json: async () => ({}) }
        : { ok: true, status: 200, json: async () => body }
    })
    render(
      <ShellVisibilityProvider>
        <AppShell brand="Test" nav={NAV}>
          <ReplayPage slug="fixture-v11" />
        </AppShell>
      </ShellVisibilityProvider>,
    )
    await screen.findByTestId("replay")
    const fetched = calls.length
    screen.getByRole("button", { name: /presentation mode/i }).click()
    await waitFor(() => expect(screen.getByTestId("app-shell")).toHaveAttribute("data-hidden", "true"))
    expect(screen.getByTestId("replay")).toHaveAttribute("data-presentation", "true")
    expect(screen.queryByText(/loading replay data/i)).toBeNull()
    expect(calls).toHaveLength(fetched)
  })
})

describe("PageHeader", () => {
  it("shows a title and an optional subtitle and no buttons", () => {
    render(<PageHeader title="A title" subtitle="A subtitle" />)
    expect(screen.getByRole("heading", { name: "A title" })).toBeInTheDocument()
    expect(screen.getByText("A subtitle")).toBeInTheDocument()
    expect(screen.queryByRole("button")).toBeNull()
  })
})
