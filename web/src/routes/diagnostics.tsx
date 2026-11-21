import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { OpsPageShell } from "@/components/ops/page-shell";
import { DiagnosticsPanel } from "@/features/diagnostics/diagnostics-panel";

export const Route = createFileRoute("/diagnostics")({
  component: DiagnosticsPage,
});

function DiagnosticsPage() {
  const { t } = useTranslation("ops");
  return (
    <OpsPageShell title={t("diagnostics.title")} hint={t("diagnostics.hint")}>
      <DiagnosticsPanel />
    </OpsPageShell>
  );
}
