import { Badge, Button, Card, Group, Loader, Stack, Table, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconArchive, IconRefresh } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { backupStatusOptions, backupStatusQueryKey } from "@/client/@tanstack/react-query.gen";
import { submitTask } from "@/client/sdk.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { formatFileSize } from "@/lib/utils";

/**
 * 备份产物列表与手动备份入口.
 * 生成是任务, 因此按钮只负责入队; 产物列表靠失效重取, 不做乐观插入.
 */
export function BackupPanel() {
  const { t, i18n } = useTranslation("ops");
  const queryClient = useQueryClient();
  const query = useQuery(backupStatusOptions());
  const runMutation = useMutation({
    mutationFn: async () => {
      await submitTask({ body: { type: "backup", kind: "all" }, throwOnError: true });
    },
    onSuccess: () => {
      notifications.show({ message: t("backup.queued"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: backupStatusQueryKey() });
    },
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("backup.runFailed")),
        color: "red",
      }),
  });

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
        <Text c="red">{extractErrorMessage(query.error, t("backup.loadFailed"))}</Text>
      </Card>
    );
  }

  const { enabled, root, entries } = query.data;
  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="wrap">
        <Group gap="sm">
          <Badge color={enabled ? "teal" : "gray"} variant="light">
            {enabled ? t("backup.enabled") : t("backup.disabled")}
          </Badge>
          <Text size="sm" c="dimmed">
            {root}
          </Text>
        </Group>
        <Group gap="xs">
          <Button
            variant="light"
            leftSection={<IconRefresh size={16} />}
            loading={query.isFetching}
            onClick={() => void query.refetch()}
          >
            {t("backup.refresh")}
          </Button>
          <Button
            leftSection={<IconArchive size={16} />}
            disabled={!enabled}
            loading={runMutation.isPending}
            onClick={() => runMutation.mutate()}
          >
            {t("backup.run")}
          </Button>
        </Group>
      </Group>

      {entries.length === 0 ? (
        <Card withBorder padding="lg">
          <Text c="dimmed">{t("backup.empty")}</Text>
        </Card>
      ) : (
        <Card withBorder padding={0}>
          <Table.ScrollContainer minWidth={640}>
            <Table verticalSpacing="sm" horizontalSpacing="md">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("backup.columns.name")}</Table.Th>
                  <Table.Th w={120}>{t("backup.columns.kind")}</Table.Th>
                  <Table.Th w={120}>{t("backup.columns.size")}</Table.Th>
                  <Table.Th w={220}>{t("backup.columns.created")}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {entries.map((entry) => (
                  <Table.Tr key={entry.name}>
                    <Table.Td>
                      <Text size="sm" ff="monospace">
                        {entry.name}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Badge variant="light">{t(`backup.kinds.${entry.kind}`)}</Badge>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{formatFileSize(entry.size_bytes)}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" c="dimmed">
                        {new Date(entry.created_at).toLocaleString(i18n.language)}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Card>
      )}
    </Stack>
  );
}
