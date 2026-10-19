"use client"

/** `/method`: Method, Reproduce it (hidden while the repo link is null) and Limitations. */
import { ErrorPanel } from "@/components/replay/error-panel"
import { PageHeader } from "@/components/shell/page-header"
import { methodSteps, reproduceTabs } from "@/lib/findings/view"
import { LimitationsGrid } from "./limitations-grid"
import { MethodSteps } from "./method-steps"
import { ReproduceTabs } from "./reproduce-tabs"
import { useFindingsData } from "./use-findings-data"

export function MethodPage() {
  const data = useFindingsData()
  if (data.state === "loading") return <p className="p-6 text-sm text-muted-foreground">Loading</p>
  if (data.state === "failed") {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <ErrorPanel title="The method data could not be used" problems={data.problems} total={data.problems.length} />
      </div>
    )
  }
  const { meta, summary } = data
  const tabs = reproduceTabs(meta)
  return (
    <div className="mx-auto max-w-5xl space-y-10 p-6">
      <PageHeader title="Method" subtitle={meta.systems_intro} />
      <MethodSteps heading={meta.method.heading} steps={methodSteps(meta)} />
      {tabs ? <ReproduceTabs heading={meta.reproduce.heading} tabs={tabs} note={meta.reproduce.note} /> : null}
      <LimitationsGrid heading={meta.limitations_heading} items={summary.caveats} />
    </div>
  )
}
