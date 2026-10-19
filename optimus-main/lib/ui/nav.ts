/** The AppShell's navigation: the four views of the replay app. Labels are chrome, not data. */
import { BookOpen, Bot, Play } from "lucide-react"
import type { LucideIcon } from "lucide-react"

export interface NavItem {
  href: string
  label: string
  icon: LucideIcon
}

export const BRAND = "Leak Lab"

export const NAV: NavItem[] = [
  { href: "/replay", label: "Replay", icon: Play },
  { href: "/agent", label: "Agent", icon: Bot },
  { href: "/method", label: "Method", icon: BookOpen },
]
