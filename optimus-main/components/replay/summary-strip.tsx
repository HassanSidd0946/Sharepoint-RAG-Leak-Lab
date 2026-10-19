import { formatSeconds } from "@/lib/replay/scale"
import type { LaneModel } from "@/lib/replay/model"

const LEAK_HINT =
  "How long the file was still served at all. It can be longer than the time to first denial: the system may serve the file again after its first denial (flapping) until it settles."
const FIRST_DENY_HINT = "Time from the confirmed change to the first read that was denied."
const NOTHING_HINT = "The system never served the flipped pair before the change, so there was nothing to revoke."

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
              <dd
                data-testid={`status-${lane.id}`}
                className="font-mono"
                title={lane.neverServed ? NOTHING_HINT : undefined}
              >
                {lane.summary.status}
              </dd>
              <dt className="text-muted-foreground" title={FIRST_DENY_HINT}>
                first_deny_s
              </dt>
              <dd data-testid={`first-deny-${lane.id}`} className="font-mono tabular-nums" title={FIRST_DENY_HINT}>
                {formatSeconds(lane.summary.first_deny_s)}
              </dd>
              <dt className="text-muted-foreground" title={LEAK_HINT}>
                leak_exposure_s <span aria-hidden>{"ⓘ"}</span>
              </dt>
              <dd data-testid={`leak-exposure-${lane.id}`} className="font-mono tabular-nums" title={LEAK_HINT}>
                {formatSeconds(lane.summary.leak_exposure_s)}
              </dd>
            </dl>
          ) : (
            <p className="text-sm text-muted-foreground">no summary in this bundle</p>
          )}
        </div>
      ))}
    </div>
  )
}
