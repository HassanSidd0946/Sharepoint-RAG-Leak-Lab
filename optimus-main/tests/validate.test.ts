import { readdirSync, readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { checkBundle, checkIndex } from "@/lib/replay/validate"
import fixture from "./fixtures/bundle.json"
import fixtureIndex from "./fixtures/index.json"

const clone = () => JSON.parse(JSON.stringify(fixture))

describe("bundle validation", () => {
  it("accepts the fixture bundle and its index", () => {
    expect(checkBundle(fixture).ok).toBe(true)
    expect(checkIndex(fixtureIndex).ok).toBe(true)
  })

  it("rejects a missing section, an unknown outcome and a stray field", () => {
    const noSummary = clone()
    delete noSummary.summary
    expect(checkBundle(noSummary).ok).toBe(false)

    const badOutcome = clone()
    badOutcome.events.find((e: { type: string }) => e.type === "read").outcome = "timeout"
    const r = checkBundle(badOutcome)
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.problems.join("\n")).toMatch(/\/events\/\d+\/outcome/)

    const extra = clone()
    extra.run.tenant_id = "x"
    expect(checkBundle(extra).ok).toBe(false)
  })

  it("validates every real bundle that ships in public/replays", () => {
    const dir = join(process.cwd(), "public", "replays")
    const files = readdirSync(dir).filter((f) => f.endsWith(".json") && f !== "index.json")
    expect(files.length).toBeGreaterThan(0)
    for (const f of files) {
      const r = checkBundle(JSON.parse(readFileSync(join(dir, f), "utf8")))
      expect(r.ok, f).toBe(true)
    }
    expect(checkIndex(JSON.parse(readFileSync(join(dir, "index.json"), "utf8"))).ok).toBe(true)
  })
})
