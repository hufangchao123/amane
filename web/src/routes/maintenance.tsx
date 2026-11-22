import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { OpsPageShell } from "@/components/ops/page-shell";
import { MaintenancePanel } from "@/features/maintenance/maintenance-panel";

export const Route = createFileRoute("/maintenance")({
  component: MaintenancePage,
});

function MaintenancePage() {
  const { t } = useTranslation("ops");
  return (
    <OpsPageShell title={t("maintenance.title")} hint={t("maintenance.hint")}>
      <MaintenancePanel />
    </OpsPageShell>
  );
}
