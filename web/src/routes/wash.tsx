import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { OpsPageShell } from "@/components/ops/page-shell";

export const Route = createFileRoute("/wash")({
  component: WashPage,
});

function WashPage() {
  const { t } = useTranslation("ops");
  return <OpsPageShell title={t("wash.title")} hint={t("wash.hint")} />;
}
