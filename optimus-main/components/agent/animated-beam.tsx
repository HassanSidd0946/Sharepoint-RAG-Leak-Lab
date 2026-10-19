"use client"

/**
 * A beam between two nodes: a curved SVG path plus a moving highlight (an SVG gradient whose offset
 * is animated). Copied in spirit from the Beam template and rewritten without `motion`: the timing
 * is fixed (lib/ui/beam.ts), so the same graph always animates the same way. The path is measured
 * from the two node elements and re-measured when the container resizes.
 */
import { useEffect, useId, useState, type RefObject } from "react"
import {
  BEAM_BASE_OPACITY,
  BEAM_CURVE_PX,
  BEAM_DURATION,
  BEAM_PULSE,
  BEAM_STAGGER_S,
  BEAM_WIDTH_PX,
} from "@/lib/ui/beam"
import type { BeamTone } from "@/lib/agent/flow"

const COLOR: Record<BeamTone, string> = {
  ok: "var(--served)",
  leak: "var(--denied)",
  denied: "var(--noanswer)",
  idle: "var(--border)",
}

interface Geometry {
  d: string
  x1: number
  x2: number
  mid: { x: number; y: number }
}

function measure(container: HTMLElement, from: HTMLElement, to: HTMLElement, bow: number): Geometry {
  const c = container.getBoundingClientRect()
  const a = from.getBoundingClientRect()
  const b = to.getBoundingClientRect()
  const x1 = a.left - c.left + a.width / 2
  const y1 = a.top - c.top + a.height / 2
  const x2 = b.left - c.left + b.width / 2
  const y2 = b.top - c.top + b.height / 2
  const mx = (x1 + x2) / 2
  const my = (y1 + y2) / 2
  const len = Math.hypot(x2 - x1, y2 - y1) || 1
  const cx = mx + (-(y2 - y1) / len) * bow
  const cy = my + ((x2 - x1) / len) * bow
  return { d: `M ${x1},${y1} Q ${cx},${cy} ${x2},${y2}`, x1, x2, mid: { x: (mx + cx) / 2, y: (my + cy) / 2 } }
}

export function AnimatedBeam({
  containerRef,
  fromRef,
  toRef,
  tone,
  index,
  from,
  to,
  label,
  bow = BEAM_CURVE_PX,
}: {
  containerRef: RefObject<HTMLElement | null>
  fromRef: RefObject<HTMLElement | null>
  toRef: RefObject<HTMLElement | null>
  tone: BeamTone
  /** Position of the beam on its path: staggers the highlight. */
  index: number
  from: string
  to: string
  label?: string
  bow?: number
}) {
  const gradient = useId()
  const [geo, setGeo] = useState<Geometry | null>(null)
  const [animate, setAnimate] = useState(true)

  useEffect(() => {
    const container = containerRef.current
    const a = fromRef.current
    const b = toRef.current
    if (!container || !a || !b) return
    const update = () => setGeo(measure(container, a, b, bow))
    update()
    setAnimate(!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches)
    if (typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(update)
    observer.observe(container)
    return () => observer.disconnect()
  }, [containerRef, fromRef, toRef, bow])

  const color = COLOR[tone]
  const moving = animate && tone !== "idle"
  return (
    <g data-testid="beam" data-from={from} data-to={to} data-tone={tone} data-label={label}>
      {geo ? (
        <>
          <defs>
            <linearGradient id={gradient} gradientUnits="userSpaceOnUse" x1={geo.x1} x2={geo.x2} y1="0" y2="0">
              <stop offset="0" stopColor={color} stopOpacity="0" />
              <stop offset="0.5" stopColor={color} />
              <stop offset="1" stopColor={color} stopOpacity="0" />
              {moving ? (
                <>
                  <animate attributeName="x1" values={`${geo.x1 - (geo.x2 - geo.x1) * BEAM_PULSE};${geo.x2}`} dur={BEAM_DURATION} begin={`${index * BEAM_STAGGER_S}s`} repeatCount="indefinite" />
                  <animate attributeName="x2" values={`${geo.x1};${geo.x2 + (geo.x2 - geo.x1) * BEAM_PULSE}`} dur={BEAM_DURATION} begin={`${index * BEAM_STAGGER_S}s`} repeatCount="indefinite" />
                </>
              ) : null}
            </linearGradient>
          </defs>
          <path d={geo.d} fill="none" stroke={color} strokeOpacity={tone === "idle" ? 1 : BEAM_BASE_OPACITY} strokeWidth={BEAM_WIDTH_PX} strokeLinecap="round" />
          {tone !== "idle" ? <path d={geo.d} fill="none" stroke={`url(#${gradient})`} strokeWidth={BEAM_WIDTH_PX * 2} strokeLinecap="round" /> : null}
          {label ? (
            <text x={geo.mid.x} y={geo.mid.y} textAnchor="middle" className="fill-muted-foreground text-[10px]" dy="-4">
              {label}
            </text>
          ) : null}
        </>
      ) : null}
    </g>
  )
}
