import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { OpsPageShell } from "@/components/ops/page-shell";
import { AuditPanel } from "@/features/audit/audit-panel";

export const Route = createFileRoute("/audit")({
  component: AuditPage,
});

function AuditPage() {
  const { t } = useTranslation("ops");
  return (
    <OpsPageShell title={t("audit.title")} hint={t("audit.hint")}>
      <AuditPanel />
    </OpsPageShell>
  );
}
