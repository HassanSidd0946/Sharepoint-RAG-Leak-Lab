"use client"

import { useState } from "react"
import { Check, Copy } from "lucide-react"
import type { TabItem } from "@/lib/findings/view"
import { cn } from "@/lib/utils"

/** Install / Run / Report with the real commands and a copy button. */
export function ReproduceTabs({ heading, tabs, note }: { heading: string; tabs: TabItem[]; note: string }) {
  const [active, setActive] = useState(0)
  const [copied, setCopied] = useState(false)
  const current = tabs[active] ?? tabs[0]
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(current.code)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return (
    <section data-testid="reproduce" className="space-y-4">
      <h2 className="text-2xl font-semibold tracking-tight">{heading}</h2>
      <div className="overflow-hidden rounded-xl border border-border bg-card shadow-card">
        <div role="tablist" className="flex items-center border-b border-border">
          {tabs.map((t, i) => (
            <button
              key={t.label}
              role="tab"
              type="button"
              aria-selected={i === active}
              onClick={() => {
                setActive(i)
                setCopied(false)
              }}
              className={cn("px-5 py-3 font-mono text-sm", i === active ? "border-b-2 border-foreground" : "text-muted-foreground")}
            >
              {t.label}
            </button>
          ))}
          <button type="button" onClick={copy} aria-label="Copy" className="ml-auto mr-3 rounded-md p-2 hover:bg-muted">
            {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
          </button>
        </div>
        <pre role="tabpanel" className="overflow-x-auto p-5 font-mono text-sm">
          {current.code}
        </pre>
      </div>
      <p className="text-sm text-muted-foreground">{note}</p>
    </section>
  )
}
