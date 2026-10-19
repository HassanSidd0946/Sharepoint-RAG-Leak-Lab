import type { StatItem } from "@/lib/findings/view"

/** The three medians side by side. Static: nothing scrolls. */
export function StatsStrip({ heading, items }: { heading: string; items: StatItem[] }) {
  return (
    <section className="border-b border-border">
      <div className="mx-auto max-w-[1200px] px-6 py-12">
        <h2 className="mb-6 text-sm font-medium text-muted-foreground">{heading}</h2>
        <dl data-testid="stats-strip" className="grid gap-6 sm:grid-cols-3">
          {items.map((item) => (
            <div key={item.label} className="rounded-xl border border-border bg-card p-5 shadow-card">
              <dt className="text-sm font-medium">{item.label}</dt>
              <dd className="mt-2 text-3xl font-semibold tracking-tight tabular-nums lg:text-4xl">{item.value}</dd>
              <dd className="mt-1 text-xs text-muted-foreground">{item.caption}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  )
}
