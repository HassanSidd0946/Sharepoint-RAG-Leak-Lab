/**
 * Fixed wording for two caveats the bundle schema has no field for. Everything else in the footer
 * (n, run, generation, commit, configuration labels) comes from the bundle and index.json.
 * (Suggestion: add a `caveats` list to schema v1.1 so this file can go.)
 */
export const TENANT_CAVEAT = "One test tenant"
export const ADMIN_CAVEAT = "Tenant admin out of scope"
