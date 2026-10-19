"use client"

/** The minimal top bar of the Findings page: the name and a few links (those whose url exists). No sign-in, no CTA. */
import Link from "next/link"
import { ThemeToggle } from "@/components/replay/theme-toggle"
import type { Link as LinkItem } from "@/lib/findings/types"

export function FindingsNav({ brand, links }: { brand: string; links: LinkItem[] }) {
  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/80 backdrop-blur">
      <nav aria-label="Main" className="mx-auto flex h-14 max-w-[1200px] items-center justify-between gap-4 px-6">
        <Link href="/" className="text-[15px] font-semibold tracking-tight">
          {brand}
        </Link>
        <div className="flex items-center gap-5 text-sm">
          {links.map((l) =>
            l.external ? (
              <a key={l.href} href={l.href} target="_blank" rel="noreferrer noopener" className="text-foreground/70 hover:text-foreground">
                {l.label}
              </a>
            ) : (
              <Link key={l.href} href={l.href} className="text-foreground/70 hover:text-foreground">
                {l.label}
              </Link>
            ),
          )}
          <ThemeToggle />
        </div>
      </nav>
    </header>
  )
}
