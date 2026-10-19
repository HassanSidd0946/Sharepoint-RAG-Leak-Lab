"use client"

/** The Findings page (`/`): everything on it comes from meta.json (text, links) and summary.json (numbers). */
import { ErrorPanel } from "@/components/replay/error-panel"
import {
  contactLink,
  findings,
  footerLinks,
  glance,
  heroActions,
  navLinks,
  statsStrip,
  systemsMeasured,
} from "@/lib/findings/view"
import { ContactCta } from "./contact-cta"
import { FindingsFooter } from "./findings-footer"
import { FindingsList } from "./findings-list"
import { FindingsNav } from "./findings-nav"
import { GlanceMetrics } from "./glance-metrics"
import { Hero } from "./hero"
import { StatsStrip } from "./stats-strip"
import { SystemsMeasured } from "./systems-measured"
import { useFindingsData } from "./use-findings-data"

export function FindingsPage() {
  const data = useFindingsData()
  if (data.state === "loading") return <p className="p-6 text-sm text-muted-foreground">Loading</p>
  if (data.state === "failed") {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <ErrorPanel title="The findings data could not be used" problems={data.problems} total={data.problems.length} />
      </div>
    )
  }
  const { meta, summary } = data
  const cta = contactLink(meta)
  return (
    <div className="min-h-screen bg-background">
      <FindingsNav brand={meta.brand} links={navLinks(meta)} />
      <main>
        <Hero
          eyebrow={meta.hero.eyebrow}
          question={meta.hero.question}
          scope={meta.hero.scope}
          actions={heroActions(meta)}
        />
        <StatsStrip heading={meta.sections.stats_heading} items={statsStrip(summary)} />
        <FindingsList heading={meta.sections.findings_heading} items={findings(meta, summary)} />
        <SystemsMeasured heading={meta.sections.systems_heading} intro={meta.systems_intro} systems={systemsMeasured(summary)} />
        <GlanceMetrics heading={meta.sections.glance_heading} items={glance(summary)} />
        {cta ? <ContactCta heading={meta.cta.heading} body={meta.cta.body} action={cta} /> : null}
      </main>
      <FindingsFooter name={meta.author} links={footerLinks(meta)} />
    </div>
  )
}
