import { Badge, Button, Card, Group, Loader, Stack, Table, Text } from "@mantine/core";
import { IconRefresh } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { diagnosticsStatusOptions } from "@/client/@tanstack/react-query.gen";
import type { CheckItemResponse } from "@/client/types.gen";
import { extractErrorMessage } from "@/lib/api-error";

/**
 * 体检结果表.
 * 状态用颜色与文字同时表达: 只靠颜色时, 色觉障碍用户无法区分通过项与失败项.
 */
export function DiagnosticsPanel() {
  const { t } = useTranslation("ops");
  const query = useQuery({ ...diagnosticsStatusOptions(), refetchOnMount: "always" });

  if (query.isPending) {
    return (
      <Group justify="center" py="xl">
        <Loader size="sm" />
      </Group>
    );
  }
  if (query.isError) {
    return (
      <Card withBorder padding="lg">
        <Text c="red">{extractErrorMessage(query.error, t("diagnostics.loadFailed"))}</Text>
      </Card>
    );
  }

  const { items, failed } = query.data;
  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Badge color={failed === 0 ? "teal" : "red"} variant="light">
          {failed === 0
            ? t("diagnostics.allPassed")
            : t("diagnostics.summary", { failed, total: items.length })}
        </Badge>
        <Button
          variant="light"
          leftSection={<IconRefresh size={16} />}
          loading={query.isFetching}
          onClick={() => void query.refetch()}
        >
          {t("diagnostics.refresh")}
        </Button>
      </Group>

      <Card withBorder padding={0}>
        <Table.ScrollContainer minWidth={640}>
          <Table verticalSpacing="sm" horizontalSpacing="md">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("diagnostics.columns.check")}</Table.Th>
                <Table.Th w={100}>{t("diagnostics.columns.state")}</Table.Th>
                <Table.Th>{t("diagnostics.columns.detail")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((item) => (
                <CheckRow key={item.key} item={item} />
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
    </Stack>
  );
}

function CheckRow({ item }: { item: CheckItemResponse }) {
  const { t } = useTranslation("ops");
  // 未知 key 回退为原始键名: 后端新增探测项时, 前端没跟上也不会把整行抹掉.
  const label = t(`diagnostics.checks.${item.key}`, { defaultValue: item.key });

  return (
    <Table.Tr>
      <Table.Td>
        <Text size="sm" fw={500}>
          {label}
        </Text>
      </Table.Td>
      <Table.Td>
        <Badge color={item.ok ? "teal" : "red"} variant="light">
          {item.ok ? t("diagnostics.passed") : t("diagnostics.failed")}
        </Badge>
      </Table.Td>
      <Table.Td>
        <Text size="sm" c="dimmed">
          {item.detail || "-"}
        </Text>
      </Table.Td>
    </Table.Tr>
  );
}
