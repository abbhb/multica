import { SettingsPage } from "@multica/views/settings";

export default async function Page({ params }: { params: Promise<{ workspaceSlug: string }> }) {
  const { workspaceSlug } = await params;
  const reconciliationHref = workspaceSlug === process.env.MULTICA_MIGRATION_WORKSPACE_SLUG
    ? `/${encodeURIComponent(workspaceSlug)}/reconciliation`
    : undefined;
  return <SettingsPage reconciliationHref={reconciliationHref} />;
}
