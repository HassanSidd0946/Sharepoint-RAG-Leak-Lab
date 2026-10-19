"use client"

/**
 * The frame around the app pages: a sidebar (top bar on narrow screens) with the navigation and the
 * theme toggle. It holds no data. Copied from the Pulse template and stripped to what this app has:
 * no search, no badges, no profile, no status pill, no right rail. It renders only its children
 * while a page asks to hide it (presentation mode).
 */
import Link from "next/link"
import { usePathname } from "next/navigation"
import type { ReactNode } from "react"
import { ThemeToggle } from "@/components/replay/theme-toggle"
import type { NavItem } from "@/lib/ui/nav"
import { cn } from "@/lib/utils"
import { useShellVisibility } from "./shell-visibility"

function NavLink({ item, active, compact }: { item: NavItem; active: boolean; compact?: boolean }) {
  const Icon = item.icon
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
        active ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
        compact && "px-2.5",
      )}
    >
      <Icon className="size-4" aria-hidden />
      {item.label}
    </Link>
  )
}

export function AppShell({ brand, nav, children }: { brand: string; nav: NavItem[]; children: ReactNode }) {
  const pathname = usePathname()
  const { hidden } = useShellVisibility()
  if (hidden) return <>{children}</>
  const isActive = (item: NavItem) => pathname === item.href || pathname.startsWith(`${item.href}/`)
  return (
    <div data-testid="app-shell" className="flex min-h-screen flex-col bg-background md:flex-row">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card md:flex">
        <div className="flex h-16 items-center border-b border-border px-5 text-[15px] font-semibold tracking-tight">{brand}</div>
        <nav aria-label="Main" className="flex-1 space-y-0.5 p-4">
          {nav.map((item) => (
            <NavLink key={item.href} item={item} active={isActive(item)} />
          ))}
        </nav>
        <div className="flex items-center justify-between border-t border-border p-4">
          <span className="text-xs text-muted-foreground">Theme</span>
          <ThemeToggle />
        </div>
      </aside>
      <header className="flex items-center gap-2 border-b border-border bg-card px-3 py-2 md:hidden">
        <span className="mr-2 text-sm font-semibold">{brand}</span>
        <nav aria-label="Main (compact)" className="flex flex-1 flex-wrap gap-1">
          {nav.map((item) => (
            <NavLink key={item.href} item={item} active={isActive(item)} compact />
          ))}
        </nav>
        <ThemeToggle />
      </header>
      <main className="min-w-0 flex-1 bg-grid">{children}</main>
    </div>
  )
}
