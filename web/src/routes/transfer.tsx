import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { OpsPageShell } from "@/components/ops/page-shell";

export const Route = createFileRoute("/transfer")({
  component: TransferPage,
});

function TransferPage() {
  const { t } = useTranslation("ops");
  return <OpsPageShell title={t("transfer.title")} hint={t("transfer.hint")} />;
}
