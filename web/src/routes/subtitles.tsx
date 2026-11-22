import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { OpsPageShell } from "@/components/ops/page-shell";
import { SubtitlesView } from "@/features/subtitles";

export const Route = createFileRoute("/subtitles")({
  component: SubtitlesPage,
});

function SubtitlesPage() {
  const { t } = useTranslation("ops");
  return (
    <OpsPageShell
      title={t("subtitles.title")}
      hint={t("subtitles.hint")}
      settingsSection="subtitles"
    >
      <SubtitlesView />
    </OpsPageShell>
  );
}
