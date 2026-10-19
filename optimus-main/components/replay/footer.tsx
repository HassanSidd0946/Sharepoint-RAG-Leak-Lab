import { formatEpoch } from "@/lib/replay/scale"
import type { AgentBundle, Bundle } from "@/lib/replay/types"

/**
 * Provenance and caveats. Every caveat is a line of the bundle (bundle.caveats, written by the
 * exporter from the run metadata); a v1.0 bundle has none and the footer says so.
 */
export function ReplayFooter({ bundle }: { bundle: Bundle | AgentBundle }) {
  const p = bundle.provenance
  return (
    <footer data-testid="footer" className="space-y-2 border-t border-border pt-3 text-xs text-muted-foreground">
      {bundle.caveats.length > 0 ? (
        <ul data-testid="caveats" className="list-disc space-y-0.5 pl-4">
          {bundle.caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      ) : (
        <p data-testid="no-caveats">This bundle carries no caveats (schema v1.0).</p>
      )}
      <p className="font-mono">
        {`run ${bundle.run.run_id} · generation ${bundle.run.generation} · exported ${formatEpoch(p.exported_at)} · commit ${p.git_commit} · exporter ${p.exporter_version}`}
      </p>
      <p className="font-mono">{`${p.journal_file} · ${p.report_file}`}</p>
    </footer>
  )
}
