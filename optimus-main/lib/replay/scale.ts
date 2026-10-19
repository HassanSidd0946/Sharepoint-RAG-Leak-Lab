/** Time axis: seconds relative to the confirmed change -> x in view units. Pure functions. */
export type AxisMode = "linear" | "symlog"

/** Within +-this many seconds the symlog axis is linear (same as the write-up figure). */
const LINEAR_WITHIN_S = 30

const symlog = (t: number): number => Math.sign(t) * Math.log1p(Math.abs(t) / LINEAR_WITHIN_S)

export function makeScale(min: number, max: number, mode: AxisMode, width: number): (t: number) => number {
  const f = mode === "linear" ? (t: number) => t : symlog
  const lo = f(min)
  const span = f(max) - lo || 1
  return (t) => ((f(t) - lo) / span) * width
}

const CANDIDATES_S = [-3600, -600, -300, -60, -10, 0, 10, 60, 300, 600, 1800, 3600, 7200]

/** Axis tick positions (seconds) that fall inside the domain. */
export function axisTicks(min: number, max: number): number[] {
  return CANDIDATES_S.filter((t) => t >= min && t <= max)
}

const MIN_S = 60

/** "+10 s", "-5 min", "0". */
export function formatRel(t: number): string {
  if (t === 0) return "0"
  const sign = t > 0 ? "+" : "-"
  const a = Math.abs(t)
  return a < MIN_S * 2 ? `${sign}${a} s` : `${sign}${a / MIN_S} min`
}

/** A duration for a clock: "10.3 s" below a minute, "45 min 43.4 s" above. */
export function formatElapsed(s: number): string {
  if (s < MIN_S) return `${s.toFixed(1)} s`
  const m = Math.floor(s / MIN_S)
  return `${m} min ${(s - m * MIN_S).toFixed(1)} s`
}

/** Seconds as given by the summary: one decimal, or an em dash when the bundle has none. */
export function formatSeconds(s: number | null | undefined): string {
  return s === null || s === undefined ? "—" : formatElapsed(s)
}

const MS_PER_S = 1000

/** Epoch seconds as an ISO-8601 UTC string. */
export function formatEpoch(s: number): string {
  return new Date(s * MS_PER_S).toISOString()
}

/** Minimum distance between two axis labels, in view units; the label at 0 is the widest. */
const LABEL_GAP = 70
const ZERO_LABEL_GAP = 130

/** Drops labels that would overlap: 0 is always kept, the rest in order of distance from it. */
export function spacedTicks(ticks: number[], scale: (t: number) => number): number[] {
  const kept: number[] = ticks.includes(0) ? [0] : []
  const byDistance = [...ticks].filter((t) => t !== 0).sort((a, b) => Math.abs(a) - Math.abs(b))
  for (const t of byDistance) {
    const clear = kept.every((k) => Math.abs(scale(t) - scale(k)) >= (k === 0 ? ZERO_LABEL_GAP : LABEL_GAP))
    if (clear) kept.push(t)
  }
  return kept.sort((a, b) => a - b)
}
