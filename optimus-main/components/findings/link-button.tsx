import Link from "next/link"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

const base =
  "inline-flex h-11 items-center justify-center gap-2 rounded-full px-6 text-sm font-medium transition-colors"
const variants = {
  primary: "bg-foreground text-background hover:bg-foreground/90",
  outline: "border border-foreground/20 hover:bg-foreground/5",
}

/** A button-looking link: internal links use next/link, external ones open in a new tab. */
export function LinkButton({
  href,
  external,
  variant = "outline",
  children,
}: {
  href: string
  external?: boolean
  variant?: keyof typeof variants
  children: ReactNode
}) {
  const className = cn(base, variants[variant])
  return external ? (
    <a href={href} target="_blank" rel="noreferrer noopener" className={className}>
      {children}
    </a>
  ) : (
    <Link href={href} className={className}>
      {children}
    </Link>
  )
}
