import { Stack, Text, Title } from "@mantine/core";
import type { ReactNode } from "react";

/** 媒体运维各页同一版心: 标题 + 一句边界说明, 内容区由页面自备. */
const PAGE_MAX_WIDTH = 1120;

interface OpsPageShellProps {
  title: string;
  hint: string;
  children?: ReactNode;
}

export function OpsPageShell({ title, hint, children }: OpsPageShellProps) {
  return (
    <Stack gap="lg" maw={PAGE_MAX_WIDTH} mx="auto" w="100%">
      <Title order={2}>{title}</Title>
      <Text c="dimmed">{hint}</Text>
      {children}
    </Stack>
  );
}
