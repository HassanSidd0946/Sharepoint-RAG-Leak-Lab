import { describe, expect, it } from "vitest"
import { CHAR_PX, LABEL_GAP_PX, placeLabels } from "@/lib/ui/axis-labels"
import { axisTicks, formatRel, makeScale } from "@/lib/replay/scale"

const VIEW = 1000
const labelsFor = (min: number, max: number) =>
  axisTicks(min, max).map((t) => ({ t, text: t === 0 ? "change confirmed" : formatRel(t) }))

function placed(min: number, max: number, mode: "linear" | "symlog", px: number) {
  const scale = makeScale(min, max, mode, VIEW)
  return placeLabels(labelsFor(min, max), (t) => (scale(t) / VIEW) * px, px)
}

describe("axis labels", () => {
  it("never overlap, at any width, on either axis, for a real-sized domain", () => {
    for (const mode of ["linear", "symlog"] as const) {
      for (const px of [300, 500, 800, 1000, 1400, 1920]) {
        const out = placed(-218, 3341, mode, px)
        for (let i = 1; i < out.length; i++) {
          const a = out[i - 1]
          const b = out[i]
          expect(a.left + a.width + LABEL_GAP_PX, `${mode} ${px}px`).toBeLessThanOrEqual(b.left)
        }
        for (const l of out) {
          expect(l.left).toBeGreaterThanOrEqual(0)
          expect(l.left + l.width).toBeLessThanOrEqual(px + 1e-6)
        }
      }
    }
  })

  it("always keeps the change-confirmed label", () => {
    for (const px of [300, 800, 1920]) {
      expect(placed(-218, 3341, "linear", px).some((l) => l.text === "change confirmed")).toBe(true)
    }
  })

  it("drops the crowded neighbours first and shows more labels when there is more room", () => {
    const narrow = placed(-218, 3341, "linear", 500).length
    const wide = placed(-218, 3341, "linear", 1920).length
    expect(wide).toBeGreaterThan(narrow)
  })

  it("estimates a label's width from its text", () => {
    const [only] = placeLabels([{ t: 0, text: "abcd" }], () => 100, 1000)
    expect(only.width).toBe(4 * CHAR_PX)
  })
})
