"use client"

/**
 * One lane as an SVG: a row of ticks per flipped pair, a thin row per control pair and (if the
 * bundle has indexer_run events for this system) a strip of indexer runs. The ticks are drawn once
 * (memoized); only the "future" mask and the playhead change while playing.
 */
import { memo } from "react"
import { CONTROL_H, DASH, INDEXER_H, LANE_PAD, LEAK_MARK_W, MIN_MARK_W, ROW_GAP, ROW_H, VIEW_W } from "@/lib/replay/layout"
import type { LaneModel, PairRow } from "@/lib/replay/model"
import type { Read } from "@/lib/replay/types"

const TONE: Record<Read["outcome"], string> = {
  served: "var(--served)",
  denied: "var(--denied)",
  error: "var(--noanswer)",
  not_indexed: "var(--noanswer)",
}

/** A lane whose system never served the flipped pair is drawn neutral: not served (green), not revoked (red). */
const NEUTRAL = "var(--noanswer)"

const MARK: Partial<Record<Read["verdict"], string>> = {
  leak: "var(--denied)",
  lockout: "var(--lockout)",
}

interface Layout {
  flipped: number[]
  control: number[]
  indexer: number | null
  height: number
}

export function layoutOf(lane: LaneModel): Layout {
  let y = LANE_PAD
  const flipped = lane.flipped.map(() => {
    const at = y
    y += ROW_H + ROW_GAP
    return at
  })
  const control = lane.control.map(() => {
    const at = y
    y += CONTROL_H + ROW_GAP
    return at
  })
  let indexer: number | null = null
  if (lane.indexerRuns.length > 0) {
    indexer = y
    y += INDEXER_H + ROW_GAP
  }
  return { flipped, control, indexer, height: y + LANE_PAD }
}

function RowTicks({
  row,
  y,
  h,
  kind,
  scale,
  neutral,
}: {
  row: PairRow
  y: number
  h: number
  kind: string
  scale: (t: number) => number
  neutral: boolean
}) {
  return (
    <g data-row={kind} data-doc={row.docLabel} data-persona={row.persona}>
      {row.reads.map((r) => {
        const x = scale(r.t_rel_s)
        const mark = MARK[r.verdict]
        return (
          <g key={r.t}>
            <line
              data-testid="tick"
              data-outcome={r.outcome}
              data-verdict={r.verdict}
              x1={x}
              x2={x}
              y1={y}
              y2={y + h}
              style={{ stroke: neutral ? NEUTRAL : TONE[r.outcome] }}
              strokeWidth={MIN_MARK_W}
              vectorEffect="non-scaling-stroke"
            />
            {mark ? (
              <rect
                data-testid="verdict-mark"
                data-verdict={r.verdict}
                x={x - LEAK_MARK_W / 2}
                y={y + h}
                width={LEAK_MARK_W}
                height={ROW_GAP / 2}
                style={{ fill: mark }}
              />
            ) : null}
          </g>
        )
      })}
    </g>
  )
}

function Marker({
  at,
  id,
  color,
  label,
  scale,
}: {
  at: number | null
  id: string
  color: string
  label: string
  scale: (t: number) => number
}) {
  if (at === null) return null
  const x = scale(at)
  return (
    <line
      data-testid={id}
      x1={x}
      x2={x}
      y1={0}
      y2="100%"
      style={{ stroke: color }}
      strokeDasharray={DASH}
      strokeWidth={MIN_MARK_W}
      vectorEffect="non-scaling-stroke"
    >
      <title>{label}</title>
    </line>
  )
}

const Ticks = memo(function Ticks({ lane, scale }: { lane: LaneModel; scale: (t: number) => number }) {
  const at = layoutOf(lane)
  return (
    <g>
      {lane.flipped.map((row, i) => (
        <RowTicks key={row.canary + row.persona} row={row} y={at.flipped[i]} h={ROW_H} kind="flipped" scale={scale} neutral={lane.neverServed} />
      ))}
      {lane.control.map((row, i) => (
        <RowTicks key={row.canary + row.persona} row={row} y={at.control[i]} h={CONTROL_H} kind="control" scale={scale} neutral={lane.neverServed} />
      ))}
      <Marker at={lane.firstDenyRel} id="marker-first-deny" color="var(--denied)" label="first denial" scale={scale} />
      <Marker at={lane.lastServedRel} id="marker-last-served" color="var(--served)" label="last served" scale={scale} />
      {at.indexer !== null
        ? lane.indexerRuns.map((run) => {
            const x0 = scale(run.t_rel_s)
            const x1 = run.t_end === null ? x0 : scale(run.t_rel_s + (run.t_end - run.t_start))
            return (
              <rect
                key={run.t_start}
                data-testid="indexer-run"
                data-status={run.status}
                x={x0}
                y={at.indexer ?? 0}
                width={Math.max(x1 - x0, MIN_MARK_W)}
                height={INDEXER_H}
                style={{ fill: "var(--indexer)" }}
              >
                <title>{`${run.index}: ${run.status}`}</title>
              </rect>
            )
          })
        : null}
    </g>
  )
})

export function LaneChart({
  lane,
  scale,
  time,
  zero,
}: {
  lane: LaneModel
  scale: (t: number) => number
  time: number
  zero: number
}) {
  const { height } = layoutOf(lane)
  const now = scale(time)
  return (
    <div className="relative">
      {lane.neverServed ? (
        <div
          data-testid={`never-served-${lane.id}`}
          className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center text-sm font-medium text-muted-foreground"
        >
          <span className="rounded bg-card/80 px-2 py-0.5">Never served — nothing to revoke</span>
        </div>
      ) : null}
    <svg
      data-testid={`lane-svg-${lane.id}`}
      viewBox={`0 0 ${VIEW_W} ${height}`}
      width="100%"
      height={height}
      preserveAspectRatio="none"
      role="img"
      aria-label={lane.label}
      className="block overflow-hidden rounded-lg bg-card"
    >
      <Ticks lane={lane} scale={scale} />
      <rect
        data-testid="future-mask"
        x={now}
        y={0}
        width={Math.max(VIEW_W - now, 0)}
        height={height}
        style={{ fill: "var(--card)" }}
      />
      <line
        x1={zero}
        x2={zero}
        y1={0}
        y2={height}
        style={{ stroke: "var(--foreground)" }}
        strokeWidth={MIN_MARK_W / 2}
        vectorEffect="non-scaling-stroke"
      />
      <line
        x1={now}
        x2={now}
        y1={0}
        y2={height}
        style={{ stroke: "var(--ring)" }}
        strokeWidth={MIN_MARK_W / 2}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  </div>
  )
}
