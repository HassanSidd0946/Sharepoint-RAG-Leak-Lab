/**
 * Validation at load time: every bundle against its schema version (ajv, JSON Schema 2020-12) and
 * index.json / playback.json against small zod shapes. Nothing is rendered from data that fails here.
 * v1.0 bundles stay loadable: they are checked against schema.v1.0.json and upgraded to the v1.1
 * shape (kind scenario_run, no caveats, no window); the UI never invents the missing values.
 */
import Ajv2020 from "ajv/dist/2020"
import type { ValidateFunction } from "ajv"
import { z } from "zod"
import schemaV10 from "./schema.v1.0.json"
import schemaV11 from "./schema.v1.1.json"
import type { AgentBundle, AnyBundle, Bundle, IndexFile, ScriptStep } from "./types"

const ajv = new Ajv2020({ allErrors: true, strict: false })
const validateV10 = ajv.compile(schemaV10)
ajv.addSchema(schemaV11)
const base = schemaV11.$id
const validateScenario = ajv.getSchema(`${base}#/$defs/scenario_run`) as ValidateFunction
const validateAgent = ajv.getSchema(`${base}#/$defs/agent_trial`) as ValidateFunction

/** How many problems the error panel lists. */
export const MAX_ERRORS_SHOWN = 8

export type Checked<T> = { ok: true; value: T } | { ok: false; problems: string[]; total: number }

const EVENT_PATH = /^\/events\/(\d+)(\/.*)?$/
const EVENT_TYPES = ["read", "mutation", "indexer_run", "phase", "poller_lag"]

/** ajv reports every failed oneOf branch of an event; keep only the branch the event type names. */
function describe(errors: NonNullable<ValidateFunction["errors"]>, data: unknown): string[] {
  const events = (data as { events?: { type?: string }[] } | null)?.events
  const out: string[] = []
  const seenEvent = new Set<string>()
  for (const e of errors) {
    const m = EVENT_PATH.exec(e.instancePath)
    if (m) {
      const index = m[1]
      const type = events?.[Number(index)]?.type
      if (typeof type !== "string" || !EVENT_TYPES.includes(type)) {
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

function failed(validate: ValidateFunction, data: unknown): Checked<never> {
  const problems = describe(validate.errors ?? [], data)
  const list = problems.length > 0 ? problems : ["the bundle does not match the schema"]
  return { ok: false, problems: list.slice(0, MAX_ERRORS_SHOWN), total: list.length }
}

function one(problem: string): Checked<never> {
  return { ok: false, problems: [problem], total: 1 }
}

/** Check a bundle of any version and kind. */
export function checkAnyBundle(data: unknown): Checked<AnyBundle> {
  const version = (data as { schema_version?: unknown } | null)?.schema_version
  if (version === "1.0") {
    if (!validateV10(data)) return failed(validateV10, data)
    const old = data as unknown as Omit<Bundle, "kind" | "caveats">
    return { ok: true, value: { ...old, kind: "scenario_run", caveats: [] } as unknown as Bundle }
  }
  if (version !== "1.1") return one(`schema_version ${JSON.stringify(version)} is not supported (1.0 and 1.1 are)`)
  const kind = (data as { kind?: unknown }).kind
  if (kind === "agent_trial") {
    return validateAgent(data) ? { ok: true, value: data as AgentBundle } : failed(validateAgent, data)
  }
  if (kind === "scenario_run") {
    return validateScenario(data) ? { ok: true, value: data as Bundle } : failed(validateScenario, data)
  }
  return one(`kind ${JSON.stringify(kind)} is not a known bundle kind`)
}

/** A benchmark replicate (the lanes page). */
export function checkBundle(data: unknown): Checked<Bundle> {
  const r = checkAnyBundle(data)
  if (!r.ok) return r
  return r.value.kind === "scenario_run" ? { ok: true, value: r.value } : one("this is an agent trial, not a benchmark replicate")
}

/** An S6 agent trial (the agent page). */
export function checkAgentBundle(data: unknown): Checked<AgentBundle> {
  const r = checkAnyBundle(data)
  if (!r.ok) return r
  return r.value.kind === "agent_trial" ? { ok: true, value: r.value } : one("this is a benchmark replicate, not an agent trial")
}

const systemId = z.enum(["setup0", "setup_a_basic", "setup_b"])
const fileName = z.string().regex(/^[A-Za-z0-9_.-]+\.json$/)
const scenarioEntry = z.object({
  kind: z.literal("scenario_run").default("scenario_run"),
  bundle: fileName,
  run_id: z.string(),
  scenario: z.string(),
  replicate: z.number().int(),
  systems: z.array(systemId),
  summary: z.array(z.object({ system: systemId, status: z.string() })),
  rank: z.number().int().nullish(),
  representative: z.boolean().optional(),
})
const agentEntry = z.object({
  kind: z.literal("agent_trial"),
  bundle: fileName,
  run_id: z.string(),
  scenario: z.enum(["S6a", "S6b", "S6c", "positive_control"]),
  arm: z.enum(["agent_naive_a", "agent_naive_b", "agent_guarded_b"]),
  trial: z.number().int(),
  validity: z.enum(["VALID", "INVALID"]),
  verdict: z.enum(["LEAK", "OK", "INVALID", "SERVED", "OVERBLOCK"]),
})
const indexShape = z.object({
  schema_version: z.string(),
  ranking: z.object({ metric: z.string() }).optional(),
  bundles: z.array(z.union([agentEntry, scenarioEntry])),
})

export function checkIndex(data: unknown): Checked<IndexFile> {
  const parsed = indexShape.safeParse(data)
  if (parsed.success) return { ok: true, value: parsed.data as IndexFile }
  const list = parsed.error.issues.map((i) => `index.json ${i.path.join(".")}: ${i.message}`)
  return { ok: false, problems: list.slice(0, MAX_ERRORS_SHOWN), total: list.length }
}

const scriptShape = z
  .array(z.object({ until_t_rel_s: z.union([z.number(), z.literal("end")]), speed: z.number().positive() }))
  .min(1)
  .refine((steps) => steps.length === 0 || steps[steps.length - 1].until_t_rel_s === "end", "the last step must end at end")
  .refine(
    (steps) =>
      steps.every(
        (s, i) =>
          i === 0 ||
          s.until_t_rel_s === "end" ||
          (steps[i - 1].until_t_rel_s as number) < s.until_t_rel_s,
      ),
    "until_t_rel_s must increase",
  )

export function checkScript(data: unknown): Checked<ScriptStep[]> {
  const parsed = scriptShape.safeParse(data)
  if (parsed.success) return { ok: true, value: parsed.data as ScriptStep[] }
  const list = parsed.error.issues.map((i) => `playback.json ${i.path.join(".")}: ${i.message}`)
  return { ok: false, problems: list.slice(0, MAX_ERRORS_SHOWN), total: list.length }
}
