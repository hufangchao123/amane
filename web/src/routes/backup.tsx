import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { OpsPageShell } from "@/components/ops/page-shell";

export const Route = createFileRoute("/backup")({
  component: BackupPage,
});

function BackupPage() {
  const { t } = useTranslation("ops");
  return <OpsPageShell title={t("backup.title")} hint={t("backup.hint")} />;
}
