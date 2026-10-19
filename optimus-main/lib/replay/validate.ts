/**
 * Validation at load time: every bundle against schema.json (ajv, JSON Schema 2020-12) and
 * index.json against a small zod shape. Nothing is rendered from data that fails here.
 */
import Ajv2020 from "ajv/dist/2020"
import { z } from "zod"
import schema from "./schema.json"
import type { Bundle, IndexFile } from "./types"

const ajv = new Ajv2020({ allErrors: true, strict: false })
const validateSchema = ajv.compile(schema)

/** How many problems the error panel lists. */
export const MAX_ERRORS_SHOWN = 8

export type Checked<T> = { ok: true; value: T } | { ok: false; problems: string[]; total: number }

const EVENT_PATH = /^\/events\/(\d+)(\/.*)?$/

/** ajv reports every failed oneOf branch of an event; keep only the branch the event's `type` names. */
function describe(errors: NonNullable<typeof validateSchema.errors>, data: unknown): string[] {
  const events = (data as { events?: { type?: string }[] } | null)?.events
  const out: string[] = []
  const seenEvent = new Set<string>()
  for (const e of errors) {
    const m = EVENT_PATH.exec(e.instancePath)
    if (m) {
      const index = m[1]
      const type = events?.[Number(index)]?.type
      if (typeof type !== "string" || !["read", "mutation", "indexer_run", "phase", "poller_lag"].includes(type)) {
        if (!seenEvent.has(index)) {
          seenEvent.add(index)
          out.push(`events[${index}]: is not a known event type`)
        }
        continue
      }
      if (e.keyword === "oneOf" || !e.schemaPath.includes(`/$defs/${type}/`)) continue
    }
    out.push(`${e.instancePath || "(bundle)"}: ${e.message ?? "invalid"}`)
  }
  return out
}

export function checkBundle(data: unknown): Checked<Bundle> {
  if (validateSchema(data)) return { ok: true, value: data as unknown as Bundle }
  const problems = describe(validateSchema.errors ?? [], data)
  const list = problems.length > 0 ? problems : ["the bundle does not match the schema"]
  return { ok: false, problems: list.slice(0, MAX_ERRORS_SHOWN), total: list.length }
}

const systemId = z.enum(["setup0", "setup_a_basic", "setup_b"])
const indexShape = z.object({
  schema_version: z.string(),
  bundles: z.array(
    z.object({
      bundle: z.string().regex(/^[A-Za-z0-9_.-]+\.json$/),
      run_id: z.string(),
      scenario: z.string(),
      replicate: z.number().int(),
      systems: z.array(systemId),
      summary: z.array(z.object({ system: systemId, status: z.string() })),
    }),
  ),
})

export function checkIndex(data: unknown): Checked<IndexFile> {
  const parsed = indexShape.safeParse(data)
  if (parsed.success) return { ok: true, value: parsed.data as IndexFile }
  const list = parsed.error.issues.map((i) => `index.json ${i.path.join(".")}: ${i.message}`)
  return { ok: false, problems: list.slice(0, MAX_ERRORS_SHOWN), total: list.length }
}
