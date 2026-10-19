import { AgentPage } from "@/components/agent/agent-page"

export default async function Page({ params }: { params: Promise<{ bundle: string }> }) {
  const { bundle } = await params
  return <AgentPage slug={bundle} />
}
