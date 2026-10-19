import type { SystemRow } from "@/lib/findings/view"

/** The three systems with their configuration caveat and their median. */
export function SystemsMeasured({ heading, intro, systems }: { heading: string; intro: string; systems: SystemRow[] }) {
  return (
    <section className="border-b border-border">
      <div className="mx-auto grid max-w-[1200px] gap-12 px-6 py-16 lg:grid-cols-2 lg:py-24">
        <div>
          <h2 className="mb-6 text-3xl font-semibold tracking-tight lg:text-4xl">{heading}</h2>
          <p className="text-lg text-muted-foreground">{intro}</p>
        </div>
        <ul className="divide-y divide-border rounded-xl border border-border bg-card shadow-card">
          {systems.map((s) => (
            <li key={s.id} data-testid="system-row" className="flex items-start justify-between gap-6 p-5">
              <div>
                <div className="font-semibold">{s.label}</div>
                <div className="mt-1 text-xs text-muted-foreground">{s.configLabel}</div>
              </div>
              <div className="text-right">
                <div className="font-mono text-lg tabular-nums">{s.median}</div>
                <div className="text-xs text-muted-foreground">{s.n}</div>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
