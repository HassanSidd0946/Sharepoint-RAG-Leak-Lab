/**
 * Shapes of public/meta.json (non-measured text and URLs) and public/replays/summary.json (the numbers),
 * validated with zod at load time. summary.json is described by docs/schemas/summary.schema.json.
 */
import { z } from "zod"

const systemId = z.enum(["setup0", "setup_a_basic", "setup_b"])
const stat = z.object({
  n: z.number().int(),
  min: z.number().nullable(),
  median: z.number().nullable(),
  max: z.number().nullable(),
})
const s6Group = z.object({
  trials: z.number().int(),
  leaks: z.number().int(),
  invalid: z.number().int(),
  leaks_via_cache_memory: z.number().int(),
  leaks_via_retrieval: z.number().int(),
})

export const summarySchema = z.object({
  schema_version: z.literal("1.0"),
  systems: z.array(z.object({ id: systemId, label: z.string(), config_label: z.string() })),
  config: z.object({ read_interval_s: z.number(), poll_cap_s: z.number(), n_consecutive: z.number() }),
  cells: z.array(
    z.object({
      scenario: z.string(),
      system: systemId,
      planned: z.number().int(),
      replicates: z.number().int(),
      first_deny: stat,
      leak_exposure: stat,
      did_not_converge: z.number().int(),
      nothing_to_revoke: z.number().int(),
    }),
  ),
  s6: z
    .object({
      naive: s6Group,
      guarded: s6Group,
      positive_control: z.object({ trials: z.number().int(), served: z.number().int() }),
    })
    .nullable(),
  totals: z.object({
    runs: z.number().int(),
    replicates: z.number().int(),
    reads: z.number().int(),
    documents: z.number().int(),
    personas: z.number().int(),
  }),
  caveats: z.array(z.string()),
})
export type Summary = z.infer<typeof summarySchema>
export type SummaryCell = Summary["cells"][number]

const url = z.string().url().nullable()
const codeStep = z.object({ title: z.string(), description: z.string(), code: z.string() })

export const metaSchema = z.object({
  brand: z.string(),
  author: z.string(),
  links: z.object({ github: url, linkedin: url, writeup: url, repo: url }),
  hero: z.object({
    eyebrow: z.string(),
    question: z.string(),
    scope: z.string(),
    actions: z.object({ replay: z.string(), writeup: z.string(), code: z.string() }),
  }),
  sections: z.object({
    stats_heading: z.string(),
    findings_heading: z.string(),
    systems_heading: z.string(),
    glance_heading: z.string(),
    findings_cta_replay: z.string(),
  }),
  findings: z.array(z.object({ title: z.string(), description: z.string() })).length(3),
  method: z.object({ heading: z.string(), steps: z.array(codeStep).min(1) }),
  reproduce: z.object({
    heading: z.string(),
    tabs: z.array(z.object({ label: z.string(), code: z.string(), clone: z.boolean().optional() })).min(1),
    note: z.string(),
  }),
  systems_intro: z.string(),
  limitations_heading: z.string(),
  cta: z.object({ heading: z.string(), body: z.string(), action_label: z.string() }),
})
export type Meta = z.infer<typeof metaSchema>

/** A link that may be shown: its url exists (a null link hides whatever shows it). */
export interface Link {
  label: string
  href: string
  external?: boolean
}
