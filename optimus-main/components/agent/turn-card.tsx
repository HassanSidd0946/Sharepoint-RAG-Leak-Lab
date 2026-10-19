import type { AgentBundle, AgentTurn } from "@/lib/replay/types"
import { cn } from "@/lib/utils"

const PATH_LABEL: Record<AgentTurn["path"], string> = {
  cache_hit: "cache hit",
  memory_hit: "memory hit",
  retrieval: "retrieval",
}

const pill = "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium"

/**
 * One turn of the trial. The verdicts are the bundle's. A leak is red, but only in a VALID trial
 * is a clean turn shown as ok: in an INVALID trial nothing is claimed, so it stays grey.
 */
export function TurnCard({
  turn,
  index,
  persona,
  invalid,
  selected,
  onSelect,
}: {
  turn: AgentTurn
  index: number
  persona: string
  invalid: boolean
  selected: boolean
  onSelect: () => void
}) {
  const leak = turn.verdict === "leak"
  return (
    <article
      data-testid={`turn-${index}`}
      data-step={turn.step}
      data-verdict={turn.verdict}
      data-selected={selected}
      className={cn(
        "space-y-2 rounded-xl border bg-card p-3 shadow-card",
        leak ? "border-[color:var(--denied)]" : "border-border",
        selected && "ring-2 ring-foreground/40",
      )}
    >
      <header className="flex items-center justify-between gap-2 text-sm">
        <span className="font-semibold">{persona}</span>
        <span className="flex items-center gap-2">
          <span className="font-mono text-xs text-muted-foreground">{`+${turn.t_rel_s.toFixed(1)} s`}</span>
          <button
            type="button"
            data-testid="show-path"
            aria-pressed={selected}
            onClick={onSelect}
            className={cn("rounded-md border px-2 py-0.5 text-xs", selected ? "border-foreground bg-foreground text-background" : "border-border hover:bg-muted")}
          >
            show path
          </button>
        </span>
      </header>
      <p className="rounded-md bg-muted px-3 py-2 text-sm">{turn.query}</p>

      <div className="flex flex-wrap items-center gap-1.5">
        <span data-testid="path-badge" data-path={turn.path} className={cn(pill, "border-border bg-secondary")}>
          {PATH_LABEL[turn.path]}
        </span>
        {turn.similarity !== null ? (
          <span data-testid="similarity" className={cn(pill, "border-border font-mono")}>
            {`similarity ${turn.similarity.toFixed(2)}`}
          </span>
        ) : null}
        <span
          data-testid="guard-decision"
          data-decision={turn.guard_decision}
          className={cn(pill, turn.guard_decision === "n/a" ? "border-border text-muted-foreground" : "border-[color:var(--indexer)]")}
        >
          {turn.guard_decision === "n/a" ? "no guard" : `guard ${turn.guard_decision}`}
        </span>
        {turn.guard_reason ? (
          <span data-testid="guard-reason" className="font-mono text-xs text-muted-foreground">
            {turn.guard_reason}
          </span>
        ) : null}
        <span
          data-testid="turn-verdict"
          className={cn(
            pill,
            "ml-auto",
            leak
              ? "border-[color:var(--denied)] bg-[color:var(--denied)] text-white"
              : turn.verdict === "ok" && !invalid
                ? "border-[color:var(--served)]"
                : "border-border text-muted-foreground",
          )}
        >
          {leak ? "leak" : invalid ? "no verdict" : turn.verdict === "ok" ? "ok" : "no answer"}
        </span>
      </div>

      {turn.source_doc_labels.length > 0 ? (
        <div className="text-xs text-muted-foreground">
          sources: <span className="font-mono">{turn.source_doc_labels.join(", ")}</span>
        </div>
      ) : null}

      {turn.delivered_canaries.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5" aria-label="delivered canaries">
          {turn.delivered_canaries.map((c) => (
            <li
              key={c.canary}
              data-testid="canary"
              data-verdict={c.verdict}
              data-expected={c.expected}
              title={`expected: ${c.expected}`}
              className={cn(
                "rounded border px-1.5 py-0.5 font-mono text-xs",
                c.verdict === "leak"
                  ? "border-[color:var(--denied)] bg-[color:var(--denied)]/15 text-[color:var(--denied)]"
                  : "border-border text-muted-foreground",
              )}
            >
              {c.canary}
            </li>
          ))}
        </ul>
      ) : null}
    </article>
  )
}

export type { AgentBundle }
