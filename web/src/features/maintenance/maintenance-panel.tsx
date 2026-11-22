import { Stack } from "@mantine/core";
import { NfoCard } from "./nfo-card";

/** 库维护: 逐个动作一张卡片, 每个动作都是任务, 页面只负责入队. */
export function MaintenancePanel() {
  return (
    <Stack gap="md">
      <NfoCard />
    </Stack>
  );
}
