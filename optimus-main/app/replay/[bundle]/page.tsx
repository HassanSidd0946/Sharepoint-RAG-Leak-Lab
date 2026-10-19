import { ReplayPage } from "@/components/replay/replay-page"

export default async function Page({ params }: { params: Promise<{ bundle: string }> }) {
  const { bundle } = await params
  return <ReplayPage slug={bundle} />
}
