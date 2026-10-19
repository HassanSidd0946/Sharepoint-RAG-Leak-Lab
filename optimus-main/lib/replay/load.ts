/** Loading from public/replays/ (same origin, static files). No backend, no other host. */
import type { AgentBundle, Bundle, IndexFile, ScriptStep } from "./types"
import { checkAgentBundle, checkBundle, checkIndex, checkScript, type Checked } from "./validate"

export const REPLAY_DIR = "/replays"

type Got = { ok: true; data: unknown } | { ok: false; why: string; status?: number }

async function getJson(url: string): Promise<Got> {
  try {
    const res = await fetch(url)
    if (!res.ok) return { ok: false, why: `${url}: HTTP ${res.status}`, status: res.status }
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

export async function loadAgentBundle(file: string): Promise<Checked<AgentBundle>> {
  const got = await getJson(`${REPLAY_DIR}/${file}`)
  return got.ok ? checkAgentBundle(got.data) : failed(got.why)
}

/**
 * The scripted playback, if the folder has one. No file (404) is not an error: manual playback only.
 * A file that is there but invalid IS an error, so a typo in it is seen and not silently ignored.
 */
export async function loadScript(): Promise<Checked<ScriptStep[]> | null> {
  const got = await getJson(`${REPLAY_DIR}/playback.json`)
  if (!got.ok) return got.status === 404 ? null : failed(got.why)
  return checkScript(got.data)
}

/** URL slug of a bundle file: the file name without .json. */
export const slugOf = (file: string): string => file.replace(/\.json$/, "")
