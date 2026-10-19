/** Loading meta.json and summary.json (static files, same origin). Both are validated; a bad file is an error. */
import { metaSchema, summarySchema, type Meta, type Summary } from "./types"

export type Loaded<T> = { ok: true; value: T } | { ok: false; problems: string[] }

async function fetchJson(url: string): Promise<{ ok: true; data: unknown } | { ok: false; why: string }> {
  try {
    const res = await fetch(url)
    if (!res.ok) return { ok: false, why: `${url}: HTTP ${res.status}` }
    return { ok: true, data: await res.json() }
  } catch (err) {
    return { ok: false, why: `${url}: ${err instanceof Error ? err.message : "could not be read"}` }
  }
}

async function load<T>(url: string, parse: (d: unknown) => Loaded<T>): Promise<Loaded<T>> {
  const got = await fetchJson(url)
  return got.ok ? parse(got.data) : { ok: false, problems: [got.why] }
}

export const loadMeta = () =>
  load<Meta>("/meta.json", (d) => {
    const r = metaSchema.safeParse(d)
    return r.success ? { ok: true, value: r.data } : { ok: false, problems: r.error.issues.map((i) => `meta.json ${i.path.join(".")}: ${i.message}`) }
  })

export const loadSummary = () =>
  load<Summary>("/replays/summary.json", (d) => {
    const r = summarySchema.safeParse(d)
    return r.success ? { ok: true, value: r.data } : { ok: false, problems: r.error.issues.map((i) => `summary.json ${i.path.join(".")}: ${i.message}`) }
  })
