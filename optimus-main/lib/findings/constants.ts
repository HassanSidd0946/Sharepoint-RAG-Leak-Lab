/** Rules of presentation for the findings pages (not data). */

/** A cell with fewer replicates than this is labelled "provisional". */
export const PROVISIONAL_BELOW = 3

/** The scenario the headline strip and the first finding show, and the others the findings use. */
export const HEADLINE_SCENARIO = "S1-entra"
export const SPGROUP_SCENARIO = "S1-spgroup"

/** Lane order, the same as the replay page. */
export const SYSTEM_ORDER = ["setup0", "setup_a_basic", "setup_b"] as const
