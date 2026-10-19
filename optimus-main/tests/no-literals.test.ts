/**
 * Guard against hard-coded or placeholder content: component files may not contain numeric
 * literals (other than 0, 1, 2, 100), numbers with units inside text, or mock-data words.
 * Numbers belong in the bundle (data) or in lib/replay (layout and playback constants).
 */
import { readdirSync, readFileSync, statSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"

const ROOTS = ["components/replay", "components/agent", "app"]
const SHARED = ["components/replay", "components/agent", "app", "lib/replay"]
// caveats are the bundle's: the wording that used to be fixed in the frontend must not come back
const OLD_CAVEATS = /(One test tenant|Tenant admin out of scope|tenant admin out of scope)/
const BANNED = /(mock|sample|lorem|faker)/i
const ALLOWED_NUMBERS = new Set(["0", "1", "2", "100"])

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f)
    return statSync(p).isDirectory() ? files(p) : /\.tsx?$/.test(p) ? [p] : []
  })
}

/** Source with comments removed, and the string contents pulled out. */
function split(src: string): { code: string; strings: string[] } {
  const strings: string[] = []
  const noComments = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1")
  const code = noComments.replace(/(["'`])((?:\\.|(?!\1)[^\\])*)\1/g, (_m, _q, body: string) => {
    strings.push(body)
    return '""'
  })
  return { code, strings }
}

const sources = ROOTS.flatMap((r) => files(join(process.cwd(), r)))
const shared = SHARED.flatMap((r) => files(join(process.cwd(), r)))

describe("component files carry no hard-coded metrics or placeholder text", () => {
  it("finds the component files", () => {
    expect(sources.length).toBeGreaterThan(0)
  })

  for (const file of sources) {
    const name = file.replace(process.cwd(), "")
    const text = readFileSync(file, "utf8")
    const { code, strings } = split(text)

    it(`${name}: no mock/sample/lorem/faker`, () => {
      expect(text).not.toMatch(BANNED)
    })

    it(`${name}: no numeric literals in code`, () => {
      const found = (code.match(/(?<![\w.$-])\d+(\.\d+)?(?![\w])/g) ?? []).filter((n) => !ALLOWED_NUMBERS.has(n))
      expect(found).toEqual([])
    })

    it(`${name}: no numbers with units inside plain strings`, () => {
      const hits = strings.filter((s) => /\b\d+(\.\d+)?\s?(s|ms|min|h|x|%|k)\b/.test(s) && !s.includes("${"))
      expect(hits).toEqual([])
    })
  }
})

describe("no caveat is written into the frontend", () => {
  for (const file of shared.filter((f) => !f.endsWith("types.generated.ts"))) {
    it(`${file.replace(process.cwd(), "")}: none of the old fixed caveat strings`, () => {
      expect(readFileSync(file, "utf8")).not.toMatch(OLD_CAVEATS)
    })
  }
})
