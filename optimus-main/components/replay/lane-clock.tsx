import { laneClock, type LaneModel } from "@/lib/replay/model"
import { formatElapsed } from "@/lib/replay/scale"

/** Elapsed time since the change while the lane still serves; freezes red at its first_deny event. */
export function LaneClock({ lane, time }: { lane: LaneModel; time: number }) {
  const clock = laneClock(lane, time)
  const stopped = clock.state === "stopped" || clock.state === "cap"
  return (
    <div
      data-testid={`clock-${lane.id}`}
      data-state={clock.state}
      className={stopped ? "font-mono tabular-nums text-[color:var(--denied)]" : "font-mono tabular-nums"}
    >
      <div className="text-lg font-semibold leading-tight">
        {clock.state === "waiting"
          ? "—"
          : clock.state === "cap"
            ? "did not stop within cap"
            : formatElapsed(clock.elapsed)}
      </div>
      <div className="text-xs font-normal text-muted-foreground">
        {clock.state === "waiting" && "before the change"}
        {clock.state === "running" && "since the change, still served"}
        {clock.state === "stopped" && "until first denial"}
        {clock.state === "cap" && `after ${formatElapsed(clock.elapsed)}`}
      </div>
    </div>
  )
}
