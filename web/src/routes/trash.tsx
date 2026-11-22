import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { OpsPageShell } from "@/components/ops/page-shell";
import { TrashPanel } from "@/features/trash/trash-panel";

export const Route = createFileRoute("/trash")({
  component: TrashPage,
});

function TrashPage() {
  const { t } = useTranslation("ops");
  return (
    <OpsPageShell title={t("trash.title")} hint={t("trash.hint")}>
      <TrashPanel />
    </OpsPageShell>
  );
}
