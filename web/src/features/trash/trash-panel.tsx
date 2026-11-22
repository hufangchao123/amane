import {
  Badge,
  Button,
  Card,
  Group,
  Loader,
  NumberInput,
  Select,
  Stack,
  Switch,
  Table,
  Text,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconRefresh, IconTrash } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { getTrashStatusOptions, getTrashStatusQueryKey } from "@/client/@tanstack/react-query.gen";
import { submitTask } from "@/client/sdk.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { formatFileSize } from "@/lib/utils";

/**
 * 历史回收站 (.amane_trash) 的规模与按保留期清理入口.
 * 清理是任务, 因此按钮只负责入队; 表格靠失效重取. 默认只统计, 真删要显式关掉开关.
 */
export function TrashPanel() {
  const { t, i18n } = useTranslation("ops");
  const queryClient = useQueryClient();
  const query = useQuery(getTrashStatusOptions());
  const [libraryId, setLibraryId] = useState<string | null>(null);
  const [keepDays, setKeepDays] = useState<number | string>(30);
  const [dryRun, setDryRun] = useState(true);

  const purgeMutation = useMutation({
    mutationFn: async () => {
      await submitTask({
        body: {
          type: "trash_purge",
          library_id: libraryId === null ? null : Number(libraryId),
          keep_days: Number(keepDays),
          dry_run: dryRun,
        },
        throwOnError: true,
      });
    },
    onSuccess: () => {
      notifications.show({ message: t("trash.queued"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: getTrashStatusQueryKey() });
    },
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("trash.runFailed")),
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
        <Text c="red">{extractErrorMessage(query.error, t("trash.loadFailed"))}</Text>
      </Card>
    );
  }

  const libraries = query.data.libraries;
  const totalFiles = query.data.total_files ?? 0;
  const totalBytes = query.data.total_bytes ?? 0;
  const libraryOptions = libraries.map((item) => ({
    value: String(item.library_id),
    label: item.name,
  }));

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="wrap">
        <Group gap="sm">
          <Badge color={totalFiles > 0 ? "orange" : "gray"} variant="light">
            {t("trash.summary", { files: totalFiles, size: formatFileSize(totalBytes) })}
          </Badge>
        </Group>
        <Button
          variant="light"
          leftSection={<IconRefresh size={16} />}
          loading={query.isFetching}
          onClick={() => void query.refetch()}
        >
          {t("trash.refresh")}
        </Button>
      </Group>

      <Card withBorder padding="md">
        <Stack gap="sm">
          <Group align="flex-end" wrap="wrap" gap="sm">
            <Select
              label={t("trash.form.library")}
              placeholder={t("trash.form.allLibraries")}
              data={libraryOptions}
              value={libraryId}
              onChange={setLibraryId}
              clearable
              w={220}
            />
            <NumberInput
              label={t("trash.form.keepDays")}
              description={t("trash.form.keepDaysHint")}
              value={keepDays}
              onChange={setKeepDays}
              min={1}
              max={3650}
              w={160}
            />
            <Switch
              label={t("trash.form.dryRun")}
              description={t("trash.form.dryRunHint")}
              checked={dryRun}
              onChange={(event) => setDryRun(event.currentTarget.checked)}
            />
            <Button
              color={dryRun ? undefined : "red"}
              leftSection={<IconTrash size={16} />}
              loading={purgeMutation.isPending}
              onClick={() => purgeMutation.mutate()}
            >
              {dryRun ? t("trash.preview") : t("trash.purge")}
            </Button>
          </Group>
          <Text size="xs" c="dimmed">
            {t("trash.noRestore")}
          </Text>
        </Stack>
      </Card>

      <Table.ScrollContainer minWidth={640}>
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("trash.columns.library")}</Table.Th>
              <Table.Th>{t("trash.columns.files")}</Table.Th>
              <Table.Th>{t("trash.columns.size")}</Table.Th>
              <Table.Th>{t("trash.columns.oldest")}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {libraries.map((item) => (
              <Table.Tr key={item.library_id}>
                <Table.Td>{item.name}</Table.Td>
                <Table.Td>{item.exists ? String(item.files) : t("trash.empty")}</Table.Td>
                <Table.Td>{formatFileSize(item.bytes)}</Table.Td>
                <Table.Td>
                  {item.oldest_days === null || item.oldest_days === undefined
                    ? "—"
                    : t("trash.days", {
                        formatted: item.oldest_days.toLocaleString(i18n.language),
                      })}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Stack>
  );
}
