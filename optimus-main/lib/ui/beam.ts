/** Timing and geometry of the animated beams (never data). Fixed values: the animation is deterministic. */

/** One pass of the moving highlight along a beam, in seconds (SVG animation `dur`). */
export const BEAM_DURATION = "2.4s"
/** Delay between the beams of one path, in seconds, so the highlight travels along the path. */
export const BEAM_STAGGER_S = 0.6
/** How far a beam bows away from the straight line between its two nodes, in px. */
export const BEAM_CURVE_PX = 30
/** Stroke width of a beam, in px. */
export const BEAM_WIDTH_PX = 2
/** Length of the moving highlight as a fraction of the beam. */
export const BEAM_PULSE = 0.25
/** Opacity of the resting line under the moving highlight. */
export const BEAM_BASE_OPACITY = 0.25
