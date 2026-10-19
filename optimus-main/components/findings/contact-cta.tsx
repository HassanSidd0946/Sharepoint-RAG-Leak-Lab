import type { Link } from "@/lib/findings/types"
import { LinkButton } from "./link-button"

/** The closing call to action. Shown only when there is a contact link. */
export function ContactCta({ heading, body, action }: { heading: string; body: string; action: Link }) {
  return (
    <section data-testid="cta" className="border-b border-border">
      <div className="mx-auto max-w-[1200px] px-6 py-16 lg:py-24">
        <div className="rounded-2xl border border-foreground/20 bg-card p-8 shadow-card lg:p-14">
          <h2 className="text-3xl font-semibold tracking-tight lg:text-5xl">{heading}</h2>
          <p className="mt-4 max-w-xl text-lg text-muted-foreground">{body}</p>
          <div className="mt-8">
            <LinkButton href={action.href} external={action.external} variant="primary">
              {action.label}
            </LinkButton>
          </div>
        </div>
      </div>
    </section>
  )
}
