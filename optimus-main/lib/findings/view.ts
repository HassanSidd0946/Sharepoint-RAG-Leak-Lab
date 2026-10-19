/**
 * Pure mapping from meta.json and summary.json to the props of the findings components. Numbers are
 * formatted here, from the summary; the components never see a raw metric. A link that is null is left
 * out (the component that would show it disappears).
 */
import { formatSeconds } from "@/lib/replay/scale"
import { HEADLINE_SCENARIO, PROVISIONAL_BELOW, SPGROUP_SCENARIO, SYSTEM_ORDER } from "./constants"
import type { Link, Meta, Summary, SummaryCell } from "./types"

export interface Figure {
  value: string
  caption: string
}
export interface FindingItem {
  number: string
  title: string
  description: string
  figures: Figure[]
}
export interface SystemRow {
  id: string
  label: string
  configLabel: string
  median: string
  n: string
}
export interface StatItem {
  label: string
  value: string
  caption: string
}
export interface GlanceItem {
  label: string
  value: number
}
export interface StepItem {
  number: string
  title: string
  description: string
  code: string
}
export interface TabItem {
  label: string
  code: string
}

const DASH = "—"
const SECONDS_PER_MINUTE = 60

/** A short duration for a cap: "10 s", "60 min" (one decimal at most, no trailing .0). */
export function shortDuration(seconds: number): string {
  const [value, unit] = seconds < SECONDS_PER_MINUTE ? [seconds, "s"] : [seconds / SECONDS_PER_MINUTE, "min"]
  return `${Number(value.toFixed(1))} ${unit}`
}

/** "n = 7", or "provisional (n = 1)" below PROVISIONAL_BELOW replicates. Every cell shows its n. */
export const nLabel = (n: number): string => (n < PROVISIONAL_BELOW ? `provisional (n = ${n})` : `n = ${n}`)

const pad = (i: number): string => String(i + 1).padStart(2, "0")

export function cell(summary: Summary, scenario: string, system: string): SummaryCell | undefined {
  return summary.cells.find((c) => c.scenario === scenario && c.system === system)
}

const labelOf = (summary: Summary, id: string): string => summary.systems.find((s) => s.id === id)?.label ?? id

const median = (c: SummaryCell | undefined): string => formatSeconds(c?.first_deny.median ?? null)

function capNote(c: SummaryCell | undefined, summary: Summary): string {
  if (!c || c.did_not_converge === 0) return ""
  return ` · ${c.did_not_converge} of ${c.replicates} did not stop within ${shortDuration(summary.config.poll_cap_s)}`
}

export function statsStrip(summary: Summary): StatItem[] {
  return SYSTEM_ORDER.map((id) => {
    const c = cell(summary, HEADLINE_SCENARIO, id)
    return {
      label: labelOf(summary, id),
      value: median(c),
      caption: `${nLabel(c?.replicates ?? 0)}${capNote(c, summary)}`,
    }
  })
}

function entraFigures(summary: Summary): Figure[] {
  return SYSTEM_ORDER.map((id) => {
    const c = cell(summary, HEADLINE_SCENARIO, id)
    return { value: median(c), caption: `${labelOf(summary, id)} · ${nLabel(c?.replicates ?? 0)}${capNote(c, summary)}` }
  })
}

function spgroupFigures(summary: Summary): Figure[] {
  return SYSTEM_ORDER.map((id) => {
    const c = cell(summary, SPGROUP_SCENARIO, id)
    if (c && c.nothing_to_revoke > 0) {
      return {
        value: "Never served",
        caption: `${labelOf(summary, id)} · nothing to revoke in ${c.nothing_to_revoke} of ${c.replicates} · ${nLabel(c.replicates)}`,
      }
    }
    return { value: median(c), caption: `${labelOf(summary, id)} · ${nLabel(c?.replicates ?? 0)}` }
  })
}

function agentFigures(summary: Summary): Figure[] {
  const s6 = summary.s6
  if (!s6) return []
  return [
    {
      value: `${s6.naive.leaks} of ${s6.naive.trials}`,
      caption: `naive agent trials that leaked · ${s6.naive.leaks_via_cache_memory} delivered from the shared cache or memory, ${s6.naive.leaks_via_retrieval} from retrieval · ${nLabel(s6.naive.trials)}`,
    },
    {
      value: `${s6.guarded.leaks} of ${s6.guarded.trials}`,
      caption: `guarded agent trials that leaked · ${nLabel(s6.guarded.trials)}`,
    },
    {
      value: `${s6.positive_control.served} of ${s6.positive_control.trials}`,
      caption: `allowed-user control trials served · ${nLabel(s6.positive_control.trials)}`,
    },
  ]
}

export function findings(meta: Meta, summary: Summary): FindingItem[] {
  const figures = [entraFigures(summary), spgroupFigures(summary), agentFigures(summary)]
  return meta.findings.map((f, i) => ({ number: pad(i), title: f.title, description: f.description, figures: figures[i] ?? [] }))
}

export function systemsMeasured(summary: Summary): SystemRow[] {
  return SYSTEM_ORDER.map((id) => {
    const sys = summary.systems.find((s) => s.id === id)
    const c = cell(summary, HEADLINE_SCENARIO, id)
    return { id, label: sys?.label ?? id, configLabel: sys?.config_label ?? "", median: median(c), n: nLabel(c?.replicates ?? 0) }
  })
}

export function glance(summary: Summary): GlanceItem[] {
  const t = summary.totals
  return [
    { label: "Runs", value: t.runs },
    { label: "Replicates", value: t.replicates },
    { label: "Reads", value: t.reads },
    { label: "Documents", value: t.documents },
    { label: "Test personas", value: t.personas },
  ]
}

export function methodSteps(meta: Meta): StepItem[] {
  return meta.method.steps.map((s, i) => ({ number: pad(i), ...s }))
}

/** `git clone <repo>` and the directory name git will create. */
export function cloneCommand(repo: string): string {
  const last = repo.replace(/\/+$/, "").split("/").pop() ?? ""
  const dir = last.replace(/\.git$/, "")
  return `git clone ${repo}\ncd ${dir}`
}

/** The Reproduce tabs, or null while the repo link is null (the whole section is hidden then). */
export function reproduceTabs(meta: Meta): TabItem[] | null {
  const repo = meta.links.repo
  if (!repo) return null
  return meta.reproduce.tabs.map((t) => ({ label: t.label, code: t.clone ? `${cloneCommand(repo)}\n${t.code}` : t.code }))
}

/** The minimal top bar: Replay always; Write-up and Code only when their link exists. */
export function navLinks(meta: Meta): Link[] {
  const out: Link[] = [{ label: "Replay", href: "/replay" }]
  if (meta.links.writeup) out.push({ label: "Write-up", href: meta.links.writeup, external: true })
  if (meta.links.repo) out.push({ label: "Code", href: meta.links.repo, external: true })
  return out
}

export interface HeroAction extends Link {
  variant: "primary" | "outline"
}

export function heroActions(meta: Meta): HeroAction[] {
  const a = meta.hero.actions
  const out: HeroAction[] = [{ label: a.replay, href: "/replay", variant: "primary" }]
  if (meta.links.writeup) out.push({ label: a.writeup, href: meta.links.writeup, external: true, variant: "outline" })
  if (meta.links.repo) out.push({ label: a.code, href: meta.links.repo, external: true, variant: "outline" })
  return out
}

/** Footer links: only those that exist. */
export function footerLinks(meta: Meta): Link[] {
  const l = meta.links
  const out: Link[] = []
  if (l.github) out.push({ label: "GitHub", href: l.github, external: true })
  if (l.linkedin) out.push({ label: "LinkedIn", href: l.linkedin, external: true })
  if (l.writeup) out.push({ label: "Write-up", href: l.writeup, external: true })
  if (l.repo) out.push({ label: "Repository", href: l.repo, external: true })
  return out
}

/** The contact call to action: shown only when there is somewhere to send the reader. */
export function contactLink(meta: Meta): Link | null {
  return meta.links.linkedin ? { label: meta.cta.action_label, href: meta.links.linkedin, external: true } : null
}

export const EMPTY = DASH
