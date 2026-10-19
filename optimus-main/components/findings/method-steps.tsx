import type { StepItem } from "@/lib/findings/view"

/** The method as numbered steps, each with the real command. */
export function MethodSteps({ heading, steps }: { heading: string; steps: StepItem[] }) {
  return (
    <section data-testid="method-steps" className="space-y-6">
      <h2 className="text-2xl font-semibold tracking-tight">{heading}</h2>
      <ol className="space-y-4">
        {steps.map((s) => (
          <li key={s.number} className="grid gap-4 rounded-xl border border-border bg-card p-5 shadow-card lg:grid-cols-[4rem_1fr_1fr]">
            <span className="font-mono text-sm text-muted-foreground">{s.number}</span>
            <div>
              <h3 className="font-semibold">{s.title}</h3>
              <p className="mt-1 text-sm text-muted-foreground">{s.description}</p>
            </div>
            <pre className="overflow-x-auto rounded-lg bg-muted p-3 font-mono text-xs">{s.code}</pre>
          </li>
        ))}
      </ol>
    </section>
  )
}
