import { render, screen, within } from "@testing-library/react"
import { describe, expect, it } from "vitest"
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

  it("removes the shell, and keeps the replay, when presentation mode starts", async () => {
    render(page())
    screen.getByRole("button", { name: /presentation mode/i }).click()
    await screen.findByTestId("replay")
    expect(screen.queryByTestId("app-shell")).toBeNull()
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull()
    expect(screen.getByTestId("speed-label")).toBeInTheDocument()
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
