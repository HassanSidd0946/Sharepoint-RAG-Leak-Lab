"use client"

import { useCountUp } from "@/lib/ui/use-count-up"
import type { GlanceItem } from "@/lib/findings/view"

function Counter({ value }: { value: number }) {
  const { ref, value: shown } = useCountUp<HTMLDivElement>(value)
  return (
    <div ref={ref} data-value={value} className="text-4xl font-semibold tabular-nums lg:text-5xl">
      {shown.toLocaleString("en-US")}
    </div>
  )
}

/** What the benchmark consists of: runs, replicates, reads, documents, personas. Nothing here is live. */
export function GlanceMetrics({ heading, items }: { heading: string; items: GlanceItem[] }) {
  return (
    <section className="border-b border-border">
      <div className="mx-auto max-w-[1200px] px-6 py-16 lg:py-24">
        <h2 className="mb-10 text-3xl font-semibold tracking-tight lg:text-4xl">{heading}</h2>
        <dl data-testid="glance" className="grid grid-cols-2 gap-6 lg:grid-cols-5">
          {items.map((item) => (
            <div key={item.label} className="rounded-xl border border-border bg-card p-5 shadow-card">
              <dd>
                <Counter value={item.value} />
              </dd>
              <dt className="mt-2 text-sm text-muted-foreground">{item.label}</dt>
            </div>
          ))}
        </dl>
      </div>
    </section>
  )
}
