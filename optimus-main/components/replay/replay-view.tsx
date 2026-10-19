"use client"

/**
 * The replay: three lanes (SharePoint, Azure AI Search, Revocation layer) from one validated bundle,
 * a shared time axis, playback, summary strip and footer. It renders the bundle's outcome /
 * expected / verdict as given; the only arithmetic here is time (playback position, scale).
 */
import { useEffect, useMemo, useState } from "react"
import { Pause, Play } from "lucide-react"
import { buildModel } from "@/lib/replay/model"
import { STAGE_H, STAGE_W, VIEW_W } from "@/lib/replay/layout"
import { SCRIPT_START_S, SPEEDS, usePlayback } from "@/lib/replay/playback"
import { axisTicks, formatRel, makeScale, spacedTicks, type AxisMode } from "@/lib/replay/scale"
import type { Bundle, IndexFile, ScriptStep } from "@/lib/replay/types"
import { cn } from "@/lib/utils"
import { BundlePicker } from "./bundle-picker"
import { LaneChart } from "./lane-chart"
import { LaneClock } from "./lane-clock"
import { ReplayFooter } from "./footer"
import { SummaryStrip } from "./summary-strip"
import { ThemeToggle } from "./theme-toggle"

const PERCENT = 100

function Legend() {
  const item = "inline-flex items-center gap-1.5"
  const chip = "inline-block h-3 w-1 rounded-[1px]"
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      <span className={item}>
        <i className={chip} style={{ background: "var(--served)" }} />
        served
      </span>
      <span className={item}>
        <i className={chip} style={{ background: "var(--denied)" }} />
        denied
      </span>
      <span className={item}>
        <i className={chip} style={{ background: "var(--noanswer)" }} />
        no answer (error or not indexed)
      </span>
      <span className={item}>
        <i className={cn(chip, "opacity-60")} style={{ background: "var(--denied)" }} />
        mark under a tick: leak (served although it should be denied)
      </span>
    </div>
  )
}

export function ReplayView({
  bundle,
  index,
  startTime,
  script,
  initialPresentation = false,
}: {
  bundle: Bundle
  index: IndexFile
  startTime?: number
  /** The scripted playback (playback.json). Used in presentation mode only. */
  script?: ScriptStep[] | null
  initialPresentation?: boolean
}) {
  const model = useMemo(() => buildModel(bundle), [bundle])
  const [axis, setAxis] = useState<AxisMode>("linear")
  const [presentation, setPresentation] = useState(initialPresentation)
  const [scripted, setScripted] = useState(true)
  const [fit, setFit] = useState(1)
  const activeScript = presentation && scripted && script ? script : null
  const play = usePlayback(
    model.domain,
    startTime ?? (initialPresentation && activeScript ? SCRIPT_START_S : undefined),
    activeScript,
  )
  const entry = index.bundles.find(
    (b) =>
      b.kind === "scenario_run" &&
      b.run_id === bundle.run.run_id &&
      b.scenario === bundle.scenario.id &&
      b.replicate === bundle.scenario.replicate,
  )
  const representative = entry?.kind === "scenario_run" && entry.representative === true
  const scale = useMemo(
    () => makeScale(model.domain.min, model.domain.max, axis, VIEW_W),
    [model.domain, axis],
  )
  const zero = scale(0)
  const ticks = useMemo(
    () => spacedTicks(axisTicks(model.domain.min, model.domain.max), scale),
    [model.domain, scale],
  )

  /** Entering presentation mode with a script goes back to the script start, paused. */
  const setMode = (on: boolean) => {
    setPresentation(on)
    if (on && script && scripted) play.restart(SCRIPT_START_S)
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      if (el && ["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName)) return
      if (e.key === " ") {
        e.preventDefault()
        play.toggle()
      } else if (e.key === "p" || e.key === "P") setMode(!presentation)
      else if (e.key === "Escape") setMode(false)
      else if ((e.key === "s" || e.key === "S") && presentation) setScripted((v) => !v)
      else if ((e.key === "r" || e.key === "R") && presentation && activeScript) play.restart(SCRIPT_START_S)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  })

  useEffect(() => {
    if (!presentation) return
    const resize = () => setFit(Math.min(window.innerWidth / STAGE_W, window.innerHeight / STAGE_H))
    resize()
    window.addEventListener("resize", resize)
    return () => window.removeEventListener("resize", resize)
  }, [presentation])

  const flipped = bundle.scenario.tracked_pairs.filter((p) => p.role === "flipped")
  const persona = (id: string) => bundle.run.personas.find((p) => p.id === id)?.display ?? id

  const body = (
    <div
      data-testid="replay"
      data-presentation={presentation}
      className={cn("mx-auto flex max-w-[1500px] flex-col gap-4 p-4 text-foreground", presentation && "h-full max-w-none gap-6 p-10 text-2xl")}
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold">{`${bundle.scenario.id} · replicate ${bundle.scenario.replicate}`}</h1>
          <p className="max-w-3xl text-sm text-muted-foreground">{bundle.scenario.description}</p>
          <p data-testid="mutation-text" className="text-sm">
            <span className="text-muted-foreground">change confirmed: </span>
            {model.mutation ? model.mutation.summary : "no mutation event in this bundle"}
          </p>
          <p className="text-xs text-muted-foreground">
            {flipped.map((p) => `${persona(p.persona)} → ${p.doc_label}`).join(" · ")}
          </p>
        </div>
        {presentation ? null : (
          <div className="flex items-center gap-2">
            <BundlePicker index={index} current={bundle} />
            <ThemeToggle />
          </div>
        )}
      </header>

      <section aria-label="lanes" className="space-y-3">
        {model.lanes.map((lane) => (
          <div key={lane.id} data-testid={`lane-${lane.id}`} className="grid grid-cols-[17rem_1fr] items-center gap-4">
            <div className="space-y-1">
              <div className="text-base font-semibold">{lane.label}</div>
              <div className="text-[0.7rem] leading-snug text-muted-foreground">{lane.configLabel}</div>
              <LaneClock lane={lane} time={play.time} />
            </div>
            <LaneChart lane={lane} scale={scale} time={play.time} zero={zero} />
          </div>
        ))}
        <div className="grid grid-cols-[17rem_1fr] gap-4">
          <div />
          <div data-testid="axis" className="relative h-10 border-t border-border text-xs text-muted-foreground">
            {ticks.map((t) => (
              <span
                key={t}
                className="absolute top-1 -translate-x-1/2 font-mono"
                style={{ left: `${(scale(t) / VIEW_W) * PERCENT}%` }}
              >
                {t === 0 ? "change confirmed" : formatRel(t)}
              </span>
            ))}
          </div>
        </div>
        {model.otherPairs > 0 ? (
          <p className="text-xs text-muted-foreground">{`${model.otherPairs} other tracked pair(s) in the bundle are not drawn`}</p>
        ) : null}
      </section>

      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <p data-testid="speed-label" className="text-sm font-medium">{`Real run, played back at ${play.speed}x`}</p>
        {representative ? (
          <p data-testid="representative-caption" className="text-sm text-muted-foreground">
            Replicate closest to the median
          </p>
        ) : null}
        {presentation ? null : <Legend />}
      </div>

      {presentation ? null : (
        <div className="flex flex-wrap items-center gap-3" data-testid="controls">
          <button
            type="button"
            onClick={play.toggle}
            aria-label={play.playing ? "Pause" : "Play"}
            className="inline-flex size-9 items-center justify-center rounded-md border border-border bg-card hover:bg-accent"
          >
            {play.playing ? <Pause className="size-4" /> : <Play className="size-4" />}
          </button>
          <input
            type="range"
            aria-label="Scrub"
            className="min-w-48 flex-1"
            min={model.domain.min}
            max={model.domain.max}
            step="any"
            value={play.time}
            onChange={(e) => play.setTime(Number(e.target.value))}
          />
          <div role="group" aria-label="Speed" className="inline-flex overflow-hidden rounded-md border border-border">
            {SPEEDS.map((s) => (
              <button
                key={s}
                type="button"
                aria-pressed={play.speed === s}
                onClick={() => play.setSpeed(s)}
                className={cn("px-3 py-1.5 text-sm font-mono", play.speed === s ? "bg-primary text-primary-foreground" : "bg-card hover:bg-accent")}
              >
                {`${s}x`}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-2 text-sm">
            time axis
            <select
              className="rounded-md border border-border bg-card px-2 py-1"
              value={axis}
              onChange={(e) => setAxis(e.target.value as AxisMode)}
            >
              <option value="linear">linear</option>
              <option value="symlog">log beyond +-30 s</option>
            </select>
          </label>
          <button
            type="button"
            onClick={() => setMode(true)}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-sm hover:bg-accent"
          >
            Presentation mode
          </button>
          <span className="text-xs text-muted-foreground">space: play/pause · P: presentation · Esc: leave</span>
        </div>
      )}

      <SummaryStrip lanes={model.lanes} />
      <ReplayFooter bundle={bundle} />
    </div>
  )

  if (!presentation) return body
  return (
    <div className="fixed inset-0 overflow-hidden bg-background">
      <div style={{ width: STAGE_W, height: STAGE_H, transform: `scale(${fit})`, transformOrigin: "top left" }}>
        {body}
      </div>
    </div>
  )
}
