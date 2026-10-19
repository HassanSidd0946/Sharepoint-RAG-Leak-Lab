import { readFileSync } from "node:fs"
import { join } from "node:path"
import { render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { FindingsPage } from "@/components/findings/findings-page"
import { MethodPage } from "@/components/findings/method-page"
import { metaSchema, summarySchema, type Meta, type Summary } from "@/lib/findings/types"
import {
  cloneCommand,
  contactLink,
  findings,
  footerLinks,
  glance,
  heroActions,
  navLinks,
  nLabel,
  reproduceTabs,
  statsStrip,
  systemsMeasured,
} from "@/lib/findings/view"
import summaryRaw from "./fixtures/summary.json"

const metaFile = JSON.parse(readFileSync(join(process.cwd(), "public", "meta.json"), "utf8"))
const meta: Meta = metaSchema.parse(metaFile)
const summary: Summary = summarySchema.parse(summaryRaw)
const withLinks = (links: Partial<Meta["links"]>): Meta => ({ ...meta, links: { ...meta.links, ...links } })
const ALL = {
  github: "https://github.com/example",
  linkedin: "https://linkedin.com/in/example",
  writeup: "https://example.org/writeup",
  repo: "https://github.com/example/the-repo.git",
}

afterEach(() => vi.unstubAllGlobals())

describe("meta.json", () => {
  it("is valid and holds no result numbers", () => {
    const text = (v: unknown, path: string, out: string[]) => {
      if (typeof v === "string") out.push(`${path}\u0000${v}`)
      else if (Array.isArray(v)) v.forEach((x, i) => text(x, `${path}[${i}]`, out))
      else if (v && typeof v === "object") Object.entries(v).forEach(([k, x]) => text(x, `${path}.${k}`, out))
    }
    const all: string[] = []
    text(metaFile, "meta", all)
    for (const entry of all) {
      const [path, value] = entry.split("\u0000")
      if (path.includes(".links.")) continue // urls
      if (/\.code$/.test(path)) {
        expect(value, path).not.toMatch(/(^|[^\w.-])\d+(\.\d+)?([^\w.-]|$)/) // commands: no stand-alone number
      } else {
        expect(value.replace("Microsoft 365", ""), path).not.toMatch(/\d/) // prose: no digit at all (the product name aside)
      }
    }
  })

  it("keeps the wording rules: Entra-group scope, basic ACL configuration, the delivery path of the leak", () => {
    const [first, second, third] = meta.findings
    expect(first.description).toMatch(/Entra-group changes/)
    expect(first.description).toMatch(/basic ACL configuration/)
    expect(second.description).toMatch(/basic ACL configuration/)
    expect(third.description).toMatch(/shared cache and memory, not from retrieval/)
    expect(third.description).toMatch(/not re-run on the cache-hit turns/)
    expect(JSON.stringify(metaFile)).not.toMatch(/returned only permitted|permission-trimmed/i)
  })
})

describe("provisional rule", () => {
  it("labels any cell below three replicates and always shows n", () => {
    expect(nLabel(1)).toBe("provisional (n = 1)")
    expect(nLabel(2)).toBe("provisional (n = 2)")
    expect(nLabel(3)).toBe("n = 3")
    expect(nLabel(7)).toBe("n = 7")
  })

  it("applies to the strip, the findings and the systems list, from the cell's own replicates", () => {
    const strip = statsStrip(summary)
    expect(strip.map((s) => s.caption)).toEqual([
      "n = 5 · 1 of 5 did not stop within 60 min",
      "n = 5",
      "provisional (n = 2)",
    ])
    expect(systemsMeasured(summary).map((s) => s.n)).toEqual(["n = 5", "n = 5", "provisional (n = 2)"])
  })
})

describe("view mapping", () => {
  it("formats the medians from the summary", () => {
    expect(statsStrip(summary).map((s) => s.value)).toEqual(["42 min 45.0 s", "1 min 33.0 s", "10.0 s"])
  })

  it("shows a system that never served the file as such, not as a time", () => {
    const f = findings(meta, summary)[1]
    expect(f.figures[1].value).toBe("Never served")
    expect(f.figures[1].caption).toContain("nothing to revoke in 3 of 3")
  })

  it("counts the agent findings from the S6 totals, with the delivery path", () => {
    const f = findings(meta, summary)[2]
    expect(f.figures[0].value).toBe("17 of 18")
    expect(f.figures[0].caption).toContain("16 delivered from the shared cache or memory, 1 from retrieval")
    expect(f.figures[1].value).toBe("0 of 9")
    expect(f.figures[2].value).toBe("9 of 9")
  })

  it("numbers the findings and gives glance totals as numbers", () => {
    expect(findings(meta, summary).map((f) => f.number)).toEqual(["01", "02", "03"])
    expect(glance(summary).map((g) => g.value)).toEqual([7, 21, 123456, 45, 7])
  })

  it("builds the clone command from the repo url", () => {
    expect(cloneCommand("https://github.com/example/the-repo.git")).toBe("git clone https://github.com/example/the-repo.git\ncd the-repo")
    expect(cloneCommand("https://github.com/example/other/")).toBe("git clone https://github.com/example/other/\ncd other")
  })
})

describe("null links hide things", () => {
  it("the meta.json in the repo has no repo, write-up or linkedin link yet", () => {
    expect(meta.links.repo).toBeNull()
    expect(meta.links.writeup).toBeNull()
  })

  it("with every link null only the Replay link and button remain, there is no Reproduce section and no contact", () => {
    const m = withLinks({ github: null, linkedin: null, writeup: null, repo: null })
    expect(navLinks(m).map((l) => l.label)).toEqual(["Replay"])
    expect(heroActions(m).map((a) => a.label)).toEqual([meta.hero.actions.replay])
    expect(footerLinks(m)).toEqual([])
    expect(reproduceTabs(m)).toBeNull()
    expect(contactLink(m)).toBeNull()
  })

  it("with every link set they all appear, and Install gets the clone command", () => {
    const m = withLinks(ALL)
    expect(navLinks(m).map((l) => l.label)).toEqual(["Replay", "Write-up", "Code"])
    expect(heroActions(m)).toHaveLength(3)
    expect(footerLinks(m).map((l) => l.label)).toEqual(["GitHub", "LinkedIn", "Write-up", "Repository"])
    expect(contactLink(m)?.href).toBe(ALL.linkedin)
    const tabs = reproduceTabs(m)
    expect(tabs?.[0].code).toBe("git clone https://github.com/example/the-repo.git\ncd the-repo\nuv sync")
    expect(tabs?.[1].code).not.toContain("git clone")
  })
})

function serve(metaData: unknown) {
  vi.stubGlobal("fetch", async (url: string) => {
    const body = String(url).endsWith("meta.json") ? metaData : summaryRaw
    return { ok: true, status: 200, json: async () => body }
  })
}

describe("the pages", () => {
  it("the Findings page renders the question, the three medians, three findings, three systems and the totals", async () => {
    serve(withLinks({ ...ALL }))
    render(<FindingsPage />)
    await screen.findByTestId("hero-question")
    expect(screen.getByTestId("hero-question")).toHaveTextContent(meta.hero.question)
    const strip = screen.getByTestId("stats-strip")
    for (const v of ["42 min 45.0 s", "1 min 33.0 s", "10.0 s"]) expect(strip).toHaveTextContent(v)
    expect(screen.getAllByTestId("finding")).toHaveLength(3)
    expect(screen.getAllByTestId("system-row")).toHaveLength(3)
    expect(screen.getByTestId("glance").querySelector("[data-value='123456']")).not.toBeNull()
    expect(screen.getByTestId("cta")).toBeInTheDocument()
  })

  it("the Findings page shows nothing about the write-up, the code or the contact while those links are null", async () => {
    serve(metaFile)
    render(<FindingsPage />)
    await screen.findByTestId("hero-question")
    const actions = screen.getByTestId("hero-actions")
    expect(within(actions).getAllByRole("link")).toHaveLength(1)
    expect(screen.queryByTestId("cta")).toBeNull()
    expect(screen.queryByText(/coming soon/i)).toBeNull()
  })

  it("has no sign-in, pricing, testimonials, certifications, logos or Live label", async () => {
    serve(withLinks({ ...ALL }))
    const { container } = render(<FindingsPage />)
    await screen.findByTestId("hero-question")
    const text = container.textContent ?? ""
    expect(text).not.toMatch(/sign in|start creating|free trial|pricing|testimonial|trusted by|SOC 2|HIPAA|GDPR/i)
    expect(text).not.toMatch(/\bLive\b/)
    expect(container.querySelector("img")).toBeNull()
  })

  it("the Method page lists the steps and the caveats, and hides Reproduce while the repo link is null", async () => {
    serve(metaFile)
    render(<MethodPage />)
    await screen.findByTestId("method-steps")
    expect(screen.getByTestId("limitations")).toHaveTextContent("fixture caveat A")
    expect(screen.queryByTestId("reproduce")).toBeNull()
  })

  it("the Method page shows Reproduce, with the clone command, once the repo link exists", async () => {
    serve(withLinks({ repo: ALL.repo }))
    render(<MethodPage />)
    const section = await screen.findByTestId("reproduce")
    expect(section).toHaveTextContent("git clone https://github.com/example/the-repo.git")
    expect(section).toHaveTextContent(meta.reproduce.note)
  })

  it("an invalid summary.json shows the error panel and no page", async () => {
    vi.stubGlobal("fetch", async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => (String(url).endsWith("meta.json") ? metaFile : { schema_version: "9" }),
    }))
    render(<FindingsPage />)
    await waitFor(() => expect(screen.getByTestId("error-panel")).toBeInTheDocument())
    expect(screen.queryByTestId("hero-question")).toBeNull()
  })
})
