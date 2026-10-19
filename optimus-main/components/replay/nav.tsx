import Link from "next/link"

/** Links between the two views: the lanes of a benchmark replicate and the S6 agent trials. */
export function Nav() {
  const link = "rounded-md border border-border bg-card px-3 py-1.5 text-sm hover:bg-accent"
  return (
    <nav className="flex gap-2" aria-label="Views">
      <Link href="/replay" className={link}>
        Revocation lanes
      </Link>
      <Link href="/agent" className={link}>
        Agent trials
      </Link>
    </nav>
  )
}
