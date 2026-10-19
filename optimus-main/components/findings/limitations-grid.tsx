import { Info } from "lucide-react"

/** One card per caveat, as the report states them. */
export function LimitationsGrid({ heading, items }: { heading: string; items: string[] }) {
  return (
    <section data-testid="limitations" className="space-y-4">
      <h2 className="text-2xl font-semibold tracking-tight">{heading}</h2>
      <ul className="grid gap-3 md:grid-cols-2">
        {items.map((c) => (
          <li key={c} className="flex gap-3 rounded-xl border border-border bg-card p-4 text-sm shadow-card">
            <Info className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
            <span>{c}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
