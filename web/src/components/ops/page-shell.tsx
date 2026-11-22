import { Group, Stack, Text, Title } from "@mantine/core";
import type { ReactNode } from "react";
import { SettingsLink } from "./settings-link";

/** 媒体运维各页同一版心: 标题 + 一句边界说明, 内容区由页面自备. */
const PAGE_MAX_WIDTH = 1120;

interface OpsPageShellProps {
  title: string;
  hint: string;
  /** 该页对应的配置段名; 给出时在标题旁给一个直达设置页的入口. */
  settingsSection?: string;
  children?: ReactNode;
}

/** 状态与配置分处两页: 页面报现状, 设置页改参数, 因此给一条直达链接. */
export function OpsPageShell({ title, hint, settingsSection, children }: OpsPageShellProps) {
  return (
    <Stack gap="lg" maw={PAGE_MAX_WIDTH} mx="auto" w="100%">
      <Group justify="space-between" align="center" wrap="wrap">
        <Title order={2}>{title}</Title>
        {settingsSection === undefined ? null : <SettingsLink section={settingsSection} />}
      </Group>
      <Text c="dimmed">{hint}</Text>
      {children}
    </Stack>
  );
}
