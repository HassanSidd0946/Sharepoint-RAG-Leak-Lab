"use client"

/**
 * Lets a page hide the AppShell completely (presentation mode on the replay page is a full-screen
 * recording stage). The default value is a no-op, so a page renders fine without a provider.
 */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react"

interface ShellVisibility {
  hidden: boolean
  setHidden: (hidden: boolean) => void
}

const Context = createContext<ShellVisibility>({ hidden: false, setHidden: () => {} })

export function ShellVisibilityProvider({ children }: { children: ReactNode }) {
  const [hidden, setHidden] = useState(false)
  const value = useMemo(() => ({ hidden, setHidden }), [hidden])
  return <Context.Provider value={value}>{children}</Context.Provider>
}

export const useShellVisibility = () => useContext(Context)

/** Hide the shell while `hide` is true; it comes back when the component unmounts. */
export function useHideShell(hide: boolean) {
  const { setHidden } = useShellVisibility()
  useEffect(() => {
    setHidden(hide)
    return () => setHidden(false)
  }, [hide, setHidden])
}
