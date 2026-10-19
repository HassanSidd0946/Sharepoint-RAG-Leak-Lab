import type { FindingItem } from "@/lib/findings/view"

/** The numbered findings: the claim, its qualifier, and the figures that back it (each with its n). */
export function FindingsList({ heading, items }: { heading: string; items: FindingItem[] }) {
  return (
    <section className="border-b border-border">
      <div className="mx-auto max-w-[1200px] px-6 py-16 lg:py-24">
        <h2 className="mb-12 text-3xl font-semibold tracking-tight lg:text-4xl">{heading}</h2>
        <ol className="space-y-16">
          {items.map((item) => (
            <li key={item.number} data-testid="finding" className="grid gap-8 lg:grid-cols-[6rem_1fr_1fr]">
              <span className="font-mono text-sm text-muted-foreground">{item.number}</span>
              <div className="space-y-4">
                <h3 className="text-2xl font-semibold tracking-tight">{item.title}</h3>
                <p className="leading-relaxed text-muted-foreground">{item.description}</p>
              </div>
              <ul className="space-y-3">
                {item.figures.map((f) => (
                  <li key={f.caption} className="rounded-xl border border-border bg-card p-4 shadow-card">
                    <div className="text-2xl font-semibold tabular-nums">{f.value}</div>
                    <div className="mt-1 text-xs text-muted-foreground">{f.caption}</div>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}
