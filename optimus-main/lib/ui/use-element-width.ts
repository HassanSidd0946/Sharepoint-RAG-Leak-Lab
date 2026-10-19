"use client"

import { useEffect, useRef, useState } from "react"
import { DEFAULT_AXIS_PX } from "./axis-labels"

/** The layout width in px of an element (offsetWidth: CSS transforms such as the presentation stage scale do not change it), kept up to date (a fixed default where ResizeObserver does not exist). */
export function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(DEFAULT_AXIS_PX)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === "undefined") return
    const update = () => setWidth(el.offsetWidth || DEFAULT_AXIS_PX)
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return { ref, width }
}
