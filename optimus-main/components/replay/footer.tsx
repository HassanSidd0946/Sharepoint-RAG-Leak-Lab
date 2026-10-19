import { ADMIN_CAVEAT, TENANT_CAVEAT } from "@/lib/replay/caveats"
import { formatEpoch } from "@/lib/replay/scale"
import type { Bundle, IndexFile } from "@/lib/replay/types"

/** Provenance and caveats. n is counted from index.json: the replicates exported for this scenario. */
export function ReplayFooter({ bundle, index }: { bundle: Bundle; index: IndexFile }) {
  const n = index.bundles.filter((b) => b.scenario === bundle.scenario.id).length
  const p = bundle.provenance
  return (
    <footer data-testid="footer" className="space-y-1 border-t border-border pt-3 text-xs text-muted-foreground">
      <p>{`${TENANT_CAVEAT} · n = ${n} replicate${n === 1 ? "" : "s"} of ${bundle.scenario.id} in this export · ${ADMIN_CAVEAT}`}</p>
      <p className="font-mono">
        {`run ${bundle.run.run_id} · generation ${bundle.run.generation} · exported ${formatEpoch(p.exported_at)} · commit ${p.git_commit} · exporter ${p.exporter_version}`}
      </p>
      <p className="font-mono">{`${p.journal_file} · ${p.report_file}`}</p>
    </footer>
  )
}
