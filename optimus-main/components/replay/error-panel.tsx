/** Shown instead of a replay when a file cannot be read or fails the schema. */
export function ErrorPanel({ title, problems, total }: { title: string; problems: string[]; total: number }) {
  return (
    <div role="alert" data-testid="error-panel" className="rounded-md border border-[color:var(--denied)] bg-card p-4">
      <h2 className="text-base font-semibold text-[color:var(--denied)]">{title}</h2>
      <ul className="mt-2 list-disc pl-5 font-mono text-sm">
        {problems.map((p) => (
          <li key={p}>{p}</li>
        ))}
      </ul>
      {total > problems.length ? (
        <p className="mt-2 text-sm text-muted-foreground">{`${total - problems.length} more not shown`}</p>
      ) : null}
    </div>
  )
}
