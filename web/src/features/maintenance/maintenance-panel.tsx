import { Stack } from "@mantine/core";
import { AvatarCard } from "./avatar-card";
import { NfoCard } from "./nfo-card";
import { MaintenanceRecentResults } from "./recent-results";
import { UpscaleCard } from "./upscale-card";

/** 库维护: 逐个动作一张卡片, 每个动作都是任务, 页面只负责入队; 结果统一在末尾一块里看. */
export function MaintenancePanel() {
  return (
    <Stack gap="md">
      <NfoCard />
      <AvatarCard />
      <UpscaleCard />
      <MaintenanceRecentResults />
    </Stack>
  );
}
