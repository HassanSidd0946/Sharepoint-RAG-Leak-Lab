/** Animation and layout constants of the findings components (never data). */

/** Scroll distance in px after which the top bar becomes a floating bar. */
export const NAV_SCROLL_PX = 20
/** How long the "at a glance" counters count up, in ms. */
export const COUNTER_MS = 1200
/** Fraction of an element that must be visible before it animates in. */
export const REVEAL_THRESHOLD = 0.2
/** Stagger between items that animate in, in ms. */
export const STAGGER_MS = 80

/** Ease-out curve for the counters (cubic). */
export const easeOut = (p: number): number => 1 - (1 - p) ** 3
