import type { HeroAction } from "@/lib/findings/view"
import { LinkButton } from "./link-button"

/** The question as the headline, the scope line under it, and the actions whose links exist. */
export function Hero({
  eyebrow,
  question,
  scope,
  actions,
}: {
  eyebrow: string
  question: string
  scope: string
  actions: HeroAction[]
}) {
  return (
    <section className="relative overflow-hidden border-b border-border bg-grid">
      <div className="mx-auto max-w-[1200px] px-6 py-24 lg:py-32">
        <p className="mb-8 flex items-center gap-3 font-mono text-sm text-muted-foreground">
          <span className="h-px w-8 bg-foreground/30" aria-hidden />
          {eyebrow}
        </p>
        <h1 data-testid="hero-question" className="max-w-4xl text-4xl font-semibold leading-[1.05] tracking-tight sm:text-5xl lg:text-7xl">
          {question}
        </h1>
        <p className="mt-8 max-w-2xl text-lg text-muted-foreground lg:text-xl">{scope}</p>
        <div className="mt-10 flex flex-wrap gap-3" data-testid="hero-actions">
          {actions.map((a) => (
            <LinkButton key={a.href} href={a.href} external={a.external} variant={a.variant}>
              {a.label}
            </LinkButton>
          ))}
        </div>
      </div>
    </section>
  )
}
