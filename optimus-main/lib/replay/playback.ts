/**
 * Playback arithmetic (pure) and the hook that drives it with requestAnimationFrame.
 *
 * Time is driven only by frame deltas multiplied by the speed, and every delta is clamped to
 * MAX_FRAME_MS, so a stalled tab or a slow frame cannot make the replay jump. In scripted mode the
 * speed follows the replay time (not the wall clock) and a frame that crosses a step boundary is
 * split there: the same bundle and script give the same sequence of speeds on every take.
 */
"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import type { ScriptStep } from "./types"

export const SPEEDS = [1, 10, 60, 300] as const
export type Speed = (typeof SPEEDS)[number]

/** Where the scripted playback starts (seconds relative to the confirmed change). */
export const SCRIPT_START_S = -3
/** The longest slice of wall-clock time one frame may advance the replay by. */
export const MAX_FRAME_MS = 100

const MS_PER_S = 1000

/** New playback time after `dtMs` of wall-clock time at `speed`, stopped at the end. */
export function advance(
  time: number,
  dtMs: number,
  speed: number,
  end: number,
): { time: number; done: boolean } {
  const next = time + (Math.min(dtMs, MAX_FRAME_MS) / MS_PER_S) * speed
  return next >= end ? { time: end, done: true } : { time: next, done: false }
}

/** The speed the script prescribes at replay time `time`. */
export function speedAt(script: readonly ScriptStep[], time: number): number {
  for (const step of script) {
    if (step.until_t_rel_s === "end" || time < step.until_t_rel_s) return step.speed
  }
  return script[script.length - 1].speed
}

/** Like `advance`, but the speed comes from the script and a frame is split at step boundaries. */
export function advanceScript(
  time: number,
  dtMs: number,
  script: readonly ScriptStep[],
  end: number,
): { time: number; done: boolean } {
  let wall = Math.min(dtMs, MAX_FRAME_MS) / MS_PER_S
  let t = time
  while (wall > 0 && t < end) {
    const speed = speedAt(script, t)
    const step = script.find((s) => s.until_t_rel_s !== "end" && t < s.until_t_rel_s)
    const boundary = step && step.until_t_rel_s !== "end" ? Math.min(step.until_t_rel_s, end) : end
    const wallToBoundary = (boundary - t) / speed
    if (wall >= wallToBoundary) {
      t = boundary
      wall -= wallToBoundary
    } else {
      t += wall * speed
      wall = 0
    }
  }
  return t >= end ? { time: end, done: true } : { time: t, done: false }
}

export function usePlayback(
  domain: { min: number; max: number },
  start?: number,
  script?: readonly ScriptStep[] | null,
) {
  const [time, setTime] = useState(start ?? domain.min)
  const [playing, setPlaying] = useState(false)
  const [manualSpeed, setSpeed] = useState<Speed>(SPEEDS[0])
  const speed = script ? speedAt(script, time) : manualSpeed
  const timeRef = useRef(time)
  timeRef.current = time
  const speedRef = useRef(manualSpeed)
  speedRef.current = manualSpeed
  const scriptRef = useRef(script)
  scriptRef.current = script

  useEffect(() => {
    if (!playing) return
    let last = performance.now()
    let raf = 0
    const frame = (now: number) => {
      const dt = now - last
      last = now
      const s = scriptRef.current
      const step = s
        ? advanceScript(timeRef.current, dt, s, domain.max)
        : advance(timeRef.current, dt, speedRef.current, domain.max)
      timeRef.current = step.time
      setTime(step.time)
      if (step.done) setPlaying(false)
      else raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => cancelAnimationFrame(raf)
  }, [playing, domain.max])

  const toggle = useCallback(() => {
    setPlaying((p) => {
      if (!p) setTime((t) => (t >= domain.max ? (scriptRef.current ? SCRIPT_START_S : domain.min) : t))
      return !p
    })
  }, [domain.max, domain.min])

  /** Pause and go back to `to` (the script start in presentation mode). */
  const restart = useCallback((to: number) => {
    setPlaying(false)
    setTime(to)
  }, [])

  return { time, setTime, playing, toggle, speed, setSpeed, restart }
}
