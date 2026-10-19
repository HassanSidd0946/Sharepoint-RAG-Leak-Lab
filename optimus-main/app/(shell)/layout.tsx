import type { ReactNode } from "react"
import { ShellFrame } from "@/components/shell/shell-frame"

export default function ShellLayout({ children }: { children: ReactNode }) {
  return <ShellFrame>{children}</ShellFrame>
}
