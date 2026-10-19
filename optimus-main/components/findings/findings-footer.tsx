import type { Link } from "@/lib/findings/types"

/** The name and the links that exist. Nothing else. */
export function FindingsFooter({ name, links }: { name: string; links: Link[] }) {
  return (
    <footer className="mx-auto flex max-w-[1200px] flex-wrap items-center justify-between gap-4 px-6 py-10 text-sm">
      <span className="font-medium">{name}</span>
      <ul className="flex flex-wrap gap-5">
        {links.map((l) => (
          <li key={l.href}>
            <a href={l.href} target="_blank" rel="noreferrer noopener" className="text-muted-foreground hover:text-foreground">
              {l.label}
            </a>
          </li>
        ))}
      </ul>
    </footer>
  )
}
