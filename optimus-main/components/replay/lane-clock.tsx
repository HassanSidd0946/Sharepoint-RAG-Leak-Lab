import { laneClock, type LaneModel } from "@/lib/replay/model"
import { formatElapsed } from "@/lib/replay/scale"

/**
 * Elapsed time since the change while the lane still serves; freezes red at its first_deny event.
 * A lane that never served the document says so in neutral grey. Once playback passes the lane's
 * last_served event the clock also says when the file was last served.
 */
export function LaneClock({ lane, time }: { lane: LaneModel; time: number }) {
  const clock = laneClock(lane, time)
  const stopped = clock.state === "stopped" || clock.state === "cap"
  const lastServed = lane.lastServedRel !== null && time >= lane.lastServedRel ? lane.lastServedRel : null
  return (
    <div
      data-testid={`clock-${lane.id}`}
      data-state={clock.state}
      className={
        stopped
          ? "font-mono tabular-nums text-[color:var(--denied)]"
          : clock.state === "never_served"
            ? "font-mono text-muted-foreground"
            : "font-mono tabular-nums"
      }
    >
      <div className="text-lg font-semibold leading-tight">
        {clock.state === "waiting"
          ? "—"
          : clock.state === "cap"
            ? "did not stop within cap"
            : clock.state === "never_served"
              ? "Never served — nothing to revoke"
              : formatElapsed(clock.elapsed)}
      </div>
      <div className="text-xs font-normal text-muted-foreground">
        {clock.state === "waiting" && "before the change"}
        {clock.state === "running" && "since the change, still served"}
        {clock.state === "stopped" && "until first denial"}
        {clock.state === "cap" && `after ${formatElapsed(clock.elapsed)}`}
      </div>
      {lastServed !== null ? (
        <div data-testid={`last-served-${lane.id}`} className="text-xs font-normal text-muted-foreground">
          {`last served at ${formatElapsed(lastServed)} after the change`}
        </div>
      ) : null}
    </div>
  )
}
