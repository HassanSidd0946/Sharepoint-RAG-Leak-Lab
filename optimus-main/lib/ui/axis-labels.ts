/**
 * Axis labels that never overlap: the label at 0 ("change confirmed") has priority, then the others by
 * distance from 0. A label is kept only if its box (estimated from its text length) fits in the axis and
 * does not touch a kept one. Pure; positions are in pixels of the axis element.
 */
export interface AxisLabel {
  t: number
  text: string
}
export interface PlacedLabel extends AxisLabel {
  left: number
  width: number
}

/** Estimated width of one character of the axis font (mono, extra small). */
export const CHAR_PX = 7.5
/** Free space kept between two labels. */
export const LABEL_GAP_PX = 14
/** The axis width used before the element has been measured (and in tests). */
export const DEFAULT_AXIS_PX = 900

export function placeLabels(labels: AxisLabel[], xOf: (t: number) => number, axisPx: number): PlacedLabel[] {
  const ordered = [...labels].sort((a, b) => (a.t === 0 ? -1 : b.t === 0 ? 1 : Math.abs(a.t) - Math.abs(b.t)))
  const kept: PlacedLabel[] = []
  for (const l of ordered) {
    const width = l.text.length * CHAR_PX
    const left = Math.min(Math.max(xOf(l.t) - width / 2, 0), Math.max(axisPx - width, 0))
    const clear = kept.every((k) => left + width + LABEL_GAP_PX <= k.left || k.left + k.width + LABEL_GAP_PX <= left)
    if (clear) kept.push({ ...l, left, width })
  }
  return kept.sort((a, b) => a.t - b.t)
}
