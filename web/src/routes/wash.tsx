import { Center, Loader, Stack } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { washStatusOptions } from "@/client/@tanstack/react-query.gen";
import { OpsPageShell } from "@/components/ops/page-shell";
import { WashCandidates } from "@/features/wash/wash-candidates";
import { WashRecords } from "@/features/wash/wash-records";
import { WashStatus } from "@/features/wash/wash-status";
import { useWashRefresh } from "@/features/wash/use-wash-refresh";

export const Route = createFileRoute("/wash")({
  component: WashPage,
});

function WashPage() {
  const { t } = useTranslation(["ops", "common"]);
  const refresh = useWashRefresh();
  const statusQuery = useQuery(washStatusOptions());

  return (
    <OpsPageShell title={t("wash.title")} hint={t("wash.hint")} settingsSection="wash">
      <Stack gap="lg">
        {statusQuery.data ? (
          <WashStatus status={statusQuery.data} onRefresh={refresh} />
        ) : (
          <Center py="md">
            <Loader size="sm" />
          </Center>
        )}
        <WashCandidates />
        <WashRecords />
      </Stack>
    </OpsPageShell>
  );
}
