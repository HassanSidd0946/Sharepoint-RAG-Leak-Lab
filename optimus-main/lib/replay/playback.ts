/** Playback arithmetic (pure) and the hook that drives it with requestAnimationFrame. */
"use client"

import { useCallback, useEffect, useRef, useState } from "react"

export const SPEEDS = [1, 10, 60, 300] as const
export type Speed = (typeof SPEEDS)[number]

const MS_PER_S = 1000

/** New playback time after `dtMs` of wall-clock time at `speed`, stopped at the end. */
export function advance(time: number, dtMs: number, speed: number, end: number): { time: number; done: boolean } {
  const next = time + (dtMs / MS_PER_S) * speed
  return next >= end ? { time: end, done: true } : { time: next, done: false }
}

export function usePlayback(domain: { min: number; max: number }, start?: number) {
  const [time, setTime] = useState(start ?? domain.min)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<Speed>(SPEEDS[0])
  const speedRef = useRef(speed)
  speedRef.current = speed
  const timeRef = useRef(time)
  timeRef.current = time

  useEffect(() => {
    if (!playing) return
    let last = performance.now()
    let raf = 0
    const frame = (now: number) => {
      const step = advance(timeRef.current, now - last, speedRef.current, domain.max)
      last = now
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
      if (!p) setTime((t) => (t >= domain.max ? domain.min : t))
      return !p
    })
  }, [domain.max, domain.min])

  return { time, setTime, playing, toggle, speed, setSpeed }
}
