"use client"

/**
 * The agent's data flow for one turn: who asked, the shared cache and memory, retrieval, the guard (guarded
 * arms) and the LLM, with a beam along the path the bundle says the turn took. It knows nothing about
 * bundles: it draws the nodes and beams it is given (see lib/agent/flow.ts).
 */
import { createRef, useMemo, useRef, type RefObject } from "react"
import type { Flow, FlowNodeId } from "@/lib/agent/flow"
import { cn } from "@/lib/utils"
import { AnimatedBeam } from "./animated-beam"

export function FlowGraph({ flow }: { flow: Flow }) {
  const container = useRef<HTMLDivElement>(null)
  const refs = useMemo(() => {
    const map = new Map<FlowNodeId, RefObject<HTMLDivElement | null>>()
    for (const n of flow.nodes) map.set(n.id, createRef<HTMLDivElement>())
    return map
  }, [flow.nodes])
  return (
    <figure data-testid="flow-graph" className="space-y-2">
      <div ref={container} className="relative grid grid-cols-3 gap-x-4 gap-y-6 rounded-xl border border-border bg-card p-5 shadow-card">
        <svg aria-hidden className="pointer-events-none absolute inset-0 size-full overflow-visible">
          {flow.beams.map((b, i) => {
            const from = refs.get(b.from)
            const to = refs.get(b.to)
            if (!from || !to) return null
            return (
              <AnimatedBeam
                key={b.id}
                containerRef={container}
                fromRef={from}
                toRef={to}
                tone={b.tone}
                index={i}
                from={b.from}
                to={b.to}
                label={b.label}
              />
            )
          })}
        </svg>
        {flow.nodes.map((n) => {
          const Icon = n.icon
          return (
            <div
              key={n.id}
              data-testid={`node-${n.id}`}
              data-active={n.active}
              style={{ gridColumn: n.col, gridRow: n.row }}
              className={cn("z-10 flex flex-col items-center gap-1 text-center", n.active ? "" : "opacity-40")}
            >
              <div
                ref={refs.get(n.id)}
                className={cn(
                  "flex size-14 items-center justify-center rounded-full border bg-background",
                  n.active ? "border-foreground/40" : "border-border",
                )}
              >
                <Icon className="size-6" aria-hidden />
              </div>
              <div className="text-xs font-medium leading-tight">{n.label}</div>
              {n.sublabel ? <div className="text-[11px] leading-tight text-muted-foreground">{n.sublabel}</div> : null}
            </div>
          )
        })}
      </div>
      <figcaption data-testid="flow-note" className="text-sm text-muted-foreground">
        {flow.note}
      </figcaption>
    </figure>
  )
}
