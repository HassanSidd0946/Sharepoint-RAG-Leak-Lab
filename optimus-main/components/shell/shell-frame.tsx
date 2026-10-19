"use client"

import type { ReactNode } from "react"
import { BRAND, NAV } from "@/lib/ui/nav"
import { AppShell } from "./app-shell"
import { ShellVisibilityProvider } from "./shell-visibility"

/** The shell with this app's navigation (a client component because the icons are components). */
export function ShellFrame({ children }: { children: ReactNode }) {
  return (
    <ShellVisibilityProvider>
      <AppShell brand={BRAND} nav={NAV}>
        {children}
      </AppShell>
    </ShellVisibilityProvider>
  )
}
