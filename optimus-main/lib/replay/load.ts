/** Loading from public/replays/ (same origin, static files). No backend, no other host. */
import type { Bundle, IndexFile } from "./types"
import { checkBundle, checkIndex, type Checked } from "./validate"

export const REPLAY_DIR = "/replays"

async function getJson(url: string): Promise<{ ok: true; data: unknown } | { ok: false; why: string }> {
  try {
    const res = await fetch(url)
    if (!res.ok) return { ok: false, why: `${url}: HTTP ${res.status}` }
    return { ok: true, data: await res.json() }
  } catch (err) {
    return { ok: false, why: `${url}: ${err instanceof Error ? err.message : "could not be read"}` }
  }
}

function failed(why: string): Checked<never> {
  return { ok: false, problems: [why], total: 1 }
}

export async function loadIndex(): Promise<Checked<IndexFile>> {
  const got = await getJson(`${REPLAY_DIR}/index.json`)
  return got.ok ? checkIndex(got.data) : failed(got.why)
}

/** `file` must be a name listed in index.json (the caller looks it up there). */
export async function loadBundle(file: string): Promise<Checked<Bundle>> {
  const got = await getJson(`${REPLAY_DIR}/${file}`)
  return got.ok ? checkBundle(got.data) : failed(got.why)
}

/** URL slug of a bundle file: the file name without .json. */
export const slugOf = (file: string): string => file.replace(/\.json$/, "")
