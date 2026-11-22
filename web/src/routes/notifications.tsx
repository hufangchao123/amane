import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { OpsPageShell } from "@/components/ops/page-shell";
import { NotifyPanel } from "@/features/notify/notify-panel";

export const Route = createFileRoute("/notifications")({
  component: NotificationsPage,
});

function NotificationsPage() {
  const { t } = useTranslation("ops");
  return (
    <OpsPageShell title={t("notify.title")} hint={t("notify.hint")} settingsSection="notify">
      <NotifyPanel />
    </OpsPageShell>
  );
}
