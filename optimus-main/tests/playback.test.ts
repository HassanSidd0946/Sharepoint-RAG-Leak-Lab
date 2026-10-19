import { describe, expect, it } from "vitest"
import { MAX_FRAME_MS, SCRIPT_START_S, advance, advanceScript, speedAt } from "@/lib/replay/playback"
import type { ScriptStep } from "@/lib/replay/types"
import { checkScript } from "@/lib/replay/validate"

const script: ScriptStep[] = [
  { until_t_rel_s: 12, speed: 1 },
  { until_t_rel_s: 360, speed: 60 },
  { until_t_rel_s: "end", speed: 300 },
]
const END = 3000

describe("scripted speed", () => {
  it("follows the replay time: 1x, 60x, 300x, with the boundary belonging to the next step", () => {
    expect([SCRIPT_START_S, 0, 11.99].map((t) => speedAt(script, t))).toEqual([1, 1, 1])
    expect([12, 100, 359.9].map((t) => speedAt(script, t))).toEqual([60, 60, 60])
    expect([360, 1000, END].map((t) => speedAt(script, t))).toEqual([300, 300, 300])
  })

  it("starts three seconds before the change", () => {
    expect(SCRIPT_START_S).toBe(-3)
  })

  it("splits a frame that crosses a boundary, so the speed changes exactly at it", () => {
    // 0.01 s of wall time at 1x reaches 12, the other 0.09 s run at 60x: 12 + 0.09 * 60
    const step = advanceScript(11.99, 100, script, END)
    expect(step.time).toBeCloseTo(12 + 0.09 * 60, 9)
    expect(step.done).toBe(false)
  })

  it("never advances more than one clamped frame, however long the frame took", () => {
    const long = advanceScript(0, 60_000, script, END)
    const clamped = advanceScript(0, MAX_FRAME_MS, script, END)
    expect(long).toEqual(clamped)
    expect(advance(0, 60_000, 10, END).time).toBeCloseTo((MAX_FRAME_MS / 1000) * 10, 9)
  })

  it("is deterministic: the same frames give the same times", () => {
    const run = () => {
      const times: number[] = []
      let t = SCRIPT_START_S
      for (let i = 0; i < 5000 && t < END; i++) {
        t = advanceScript(t, 16.7, script, END).time
        times.push(t)
      }
      return times
    }
    expect(run()).toEqual(run())
  })

  it("stops at the end of the bundle", () => {
    expect(advanceScript(END - 0.01, 100, script, END)).toEqual({ time: END, done: true })
  })
})

describe("script validation", () => {
  it("accepts a rising script that ends at end", () => {
    expect(checkScript(script).ok).toBe(true)
  })

  it("rejects a script that does not end at end, does not rise, or has a zero speed", () => {
    expect(checkScript([{ until_t_rel_s: 12, speed: 1 }]).ok).toBe(false)
    expect(checkScript([{ until_t_rel_s: 30, speed: 1 }, { until_t_rel_s: 12, speed: 60 }, { until_t_rel_s: "end", speed: 300 }]).ok).toBe(false)
    expect(checkScript([{ until_t_rel_s: "end", speed: 0 }]).ok).toBe(false)
    expect(checkScript([]).ok).toBe(false)
  })
})
