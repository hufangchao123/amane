import { Alert, Group, Stack, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { transferStatusOptions } from "@/client/@tanstack/react-query.gen";
import { OpsPageShell } from "@/components/ops/page-shell";
import { DirPicker } from "@/features/transfer/dir-picker";
import { QrLogin } from "@/features/transfer/qr-login";
import { RecentItems } from "@/features/transfer/recent-items";
import { RunTransferButton } from "@/features/transfer/run-button";
import { TransferStatusCards } from "@/features/transfer/status-cards";

export const Route = createFileRoute("/transfer")({
  component: TransferPage,
});

function TransferPage() {
  const { t } = useTranslation("ops");
  const statusQuery = useQuery(transferStatusOptions());
  const status = statusQuery.data;

  return (
    <OpsPageShell title={t("transfer.title")} hint={t("transfer.hint")} settingsSection="transfer">
      <Stack gap="lg">
        <Group justify="space-between" align="center">
          <Title order={4}>{t("transfer.statusTitle")}</Title>
          <RunTransferButton />
        </Group>
        {status?.reason != null && (
          <Alert color="yellow" variant="light">
            {status.reason}
          </Alert>
        )}
        {status !== undefined && <TransferStatusCards status={status} />}
        <QrLogin />
        <DirPicker />
        <RecentItems />
      </Stack>
    </OpsPageShell>
  );
}
