import { Badge, Paper, SimpleGrid, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { TransferStatusResponse } from "@/client/types.gen";

import { COUNTED_STATES } from "./state";

interface TransferStatusCardsProps {
  status: TransferStatusResponse;
}

/** 一张卡一个数字: 已启用之外的四格与 `transfer_items` 的四种状态一一对应. */
export function TransferStatusCards({ status }: TransferStatusCardsProps) {
  const { t } = useTranslation("ops");
  return (
    <SimpleGrid cols={{ base: 2, sm: 5 }} spacing="sm">
      <StatCard
        label={t("transfer.serviceLabel")}
        value={
          <Badge color={status.enabled ? "teal" : "gray"} variant="light">
            {status.enabled ? t("transfer.enabledOn") : t("transfer.enabledOff")}
          </Badge>
        }
      />
      {COUNTED_STATES.map((state) => (
        <StatCard key={state} label={t(`transfer.state.${state}`)} value={status[state]} />
      ))}
    </SimpleGrid>
  );
}

function StatCard({ label, value }: { label: string; value: number | string | ReactNode }) {
  return (
    <Paper withBorder p="md" radius="md">
      <Stack gap={4}>
        <Text size="xs" c="dimmed">
          {label}
        </Text>
        <Text fw={600} size="xl">
          {value}
        </Text>
      </Stack>
    </Paper>
  );
}
