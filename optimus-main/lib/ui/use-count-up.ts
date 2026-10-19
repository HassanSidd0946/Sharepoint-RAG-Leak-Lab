"use client"

import { useEffect, useRef, useState } from "react"
import { COUNTER_MS, REVEAL_THRESHOLD, easeOut } from "./animation"

/**
 * A number that counts up to `target` once its element scrolls into view. Without IntersectionObserver
 * (tests, old browsers) or with reduced motion it simply is the target: the final value is never hidden.
 */
export function useCountUp<T extends HTMLElement>(target: number) {
  const ref = useRef<T>(null)
  const [value, setValue] = useState(target)

  useEffect(() => {
    const el = ref.current
    const reduced = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    if (!el || typeof IntersectionObserver === "undefined" || reduced) {
      setValue(target)
      return
    }
    setValue(0)
    let raf = 0
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return
        observer.disconnect()
        const start = performance.now()
        const frame = (now: number) => {
          const p = Math.min((now - start) / COUNTER_MS, 1)
          setValue(Math.round(target * easeOut(p)))
          if (p < 1) raf = requestAnimationFrame(frame)
        }
        raf = requestAnimationFrame(frame)
      },
      { threshold: REVEAL_THRESHOLD },
    )
    observer.observe(el)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(raf)
    }
  }, [target])

  return { ref, value }
}
