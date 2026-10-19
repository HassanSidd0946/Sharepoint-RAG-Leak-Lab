import { formatSeconds } from "@/lib/replay/scale"
import type { LaneModel } from "@/lib/replay/model"

/** Straight from bundle.summary: the UI never recomputes these. */
export function SummaryStrip({ lanes }: { lanes: LaneModel[] }) {
  return (
    <div data-testid="summary-strip" className="grid gap-3 md:grid-cols-3">
      {lanes.map((lane) => (
        <div key={lane.id} className="rounded-md border border-border bg-card p-3">
          <div className="text-sm font-semibold">{lane.label}</div>
          {lane.summary ? (
            <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 text-sm">
              <dt className="text-muted-foreground">status</dt>
              <dd data-testid={`status-${lane.id}`} className="font-mono">
                {lane.summary.status}
              </dd>
              <dt className="text-muted-foreground">first_deny_s</dt>
              <dd className="font-mono tabular-nums">{formatSeconds(lane.summary.first_deny_s)}</dd>
              <dt className="text-muted-foreground">leak_exposure_s</dt>
              <dd className="font-mono tabular-nums">{formatSeconds(lane.summary.leak_exposure_s)}</dd>
            </dl>
          ) : (
            <p className="text-sm text-muted-foreground">no summary in this bundle</p>
          )}
        </div>
      ))}
    </div>
  )
}
