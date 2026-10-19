import { readdirSync, readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { checkAnyBundle, checkBundle, checkIndex, checkScript } from "@/lib/replay/validate"
import fixture from "./fixtures/bundle.v10.json"
import fixtureIndex from "./fixtures/index.v10.json"

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

  it("validates every real bundle, the index and the playback script that ship in public/replays", () => {
    const dir = join(process.cwd(), "public", "replays")
    const files = readdirSync(dir).filter((f) => f.endsWith(".json") && !["index.json", "playback.json"].includes(f))
    expect(files.length).toBeGreaterThan(0)
    const kinds = new Set<string>()
    for (const f of files) {
      const r = checkAnyBundle(JSON.parse(readFileSync(join(dir, f), "utf8")))
      expect(r.ok, f).toBe(true)
      if (r.ok) kinds.add(r.value.kind)
    }
    expect([...kinds].sort()).toEqual(["agent_trial", "scenario_run"])
    expect(checkIndex(JSON.parse(readFileSync(join(dir, "index.json"), "utf8"))).ok).toBe(true)
    expect(checkScript(JSON.parse(readFileSync(join(dir, "playback.json"), "utf8"))).ok).toBe(true)
  })
})
