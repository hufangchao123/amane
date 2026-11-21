import {
  Badge,
  Button,
  Center,
  Checkbox,
  Group,
  Loader,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconRestore, IconTrash } from "@tabler/icons-react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  purgeWashRecordsMutation,
  restoreWashRecordsMutation,
  washRecordsOptions,
} from "@/client/@tanstack/react-query.gen";
import type { WashRecordItem, WashRecordKind, WashRecordState } from "@/client/types.gen";
import { ListPagination } from "@/components/common/list-pagination";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { formatFileSize } from "@/lib/utils";
import { pathName } from "./path-name";
import { useWashRefresh } from "./use-wash-refresh";

const PAGE_SIZE = 20;

const KIND_LABEL: Record<WashRecordKind, "wash.kind.local" | "wash.kind.strm"> = {
  local: "wash.kind.local",
  strm: "wash.kind.strm",
};
const STATE_LABEL: Record<
  WashRecordState,
  "wash.state.trashed" | "wash.state.restored" | "wash.state.purged"
> = {
  trashed: "wash.state.trashed",
  restored: "wash.state.restored",
  purged: "wash.state.purged",
};
const STATE_COLOR: Record<WashRecordState, string> = {
  trashed: "orange",
  restored: "teal",
  purged: "gray",
};

/** 回收站表: 可勾选在回收站里的记录, 还原或清理.
 *
 * 已还原/已清理的记录也留在表里: 它们解释了「这份文件去哪了」, 而表本身按时间倒序,
 * 老记录会自然沉底, 不必为历史另做一个视图.
 */
export function WashRecords() {
  const { t, i18n } = useTranslation(["ops", "common"]);
  const refresh = useWashRefresh();
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const query = useQuery(
    washRecordsOptions({
      query: {
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
        sort_by: "created_at",
        order: "desc",
      },
    }),
  );
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const selectable = items.filter((item) => item.state === "trashed");
  const allSelected = selectable.length > 0 && selectable.every((item) => selected.has(item.id));

  function toggle(id: number) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function togglePage() {
    setSelected((current) => {
      const next = new Set(current);
      if (allSelected) for (const item of selectable) next.delete(item.id);
      else for (const item of selectable) next.add(item.id);
      return next;
    });
  }

  function changePage(next: number) {
    setPage(next);
    // 跨页保留选中会让「还原」动到看不见的行, 翻页即清空.
    setSelected(new Set());
  }

  const restore = useMutation({
    ...restoreWashRecordsMutation(),
    onSuccess: () => {
      notifications.show({ message: t("wash.restoreStarted"), color: "blue" });
      setSelected(new Set());
      refresh();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });
  const purge = useMutation({
    ...purgeWashRecordsMutation(),
    onSuccess: () => {
      notifications.show({ message: t("wash.purgeStarted"), color: "blue" });
      setSelected(new Set());
      refresh();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  function startRestore() {
    restore.mutate({ body: { ids: [...selected] } });
  }

  async function startPurge() {
    const ok = await confirm({
      title: t("wash.confirmPurgeTitle"),
      message: t("wash.confirmPurgeMessage"),
    });
    if (!ok) return;
    purge.mutate({ body: { ids: [...selected] } });
  }

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <Title order={4}>{t("wash.recordTitle")}</Title>
        <Group gap="xs">
          <Text size="sm" c="dimmed">
            {t("wash.selected", { count: selected.size })}
          </Text>
          <Button
            size="xs"
            variant="default"
            leftSection={<IconRestore size={14} />}
            disabled={selected.size === 0}
            loading={restore.isPending}
            onClick={startRestore}
          >
            {t("wash.restore")}
          </Button>
          <Button
            size="xs"
            color="red"
            leftSection={<IconTrash size={14} />}
            disabled={selected.size === 0}
            loading={purge.isPending}
            onClick={() => void startPurge()}
          >
            {t("wash.purge")}
          </Button>
        </Group>
      </Group>
      {query.isPending ? (
        <Center py="md">
          <Loader size="sm" />
        </Center>
      ) : items.length === 0 ? (
        <Text c="dimmed" size="sm">
          {t("wash.recordEmpty")}
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={840}>
          <Table highlightOnHover verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={36}>
                  <Checkbox
                    aria-label={t("wash.column.state")}
                    checked={allSelected}
                    indeterminate={!allSelected && selectable.some((item) => selected.has(item.id))}
                    onChange={togglePage}
                  />
                </Table.Th>
                <Table.Th>{t("wash.column.path")}</Table.Th>
                <Table.Th>{t("wash.column.keep")}</Table.Th>
                <Table.Th>{t("wash.column.kind")}</Table.Th>
                <Table.Th>{t("wash.column.size")}</Table.Th>
                <Table.Th>{t("wash.column.state")}</Table.Th>
                <Table.Th>{t("wash.column.created")}</Table.Th>
                <Table.Th>{t("wash.column.expires")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((item) => (
                <Table.Tr key={item.id}>
                  <Table.Td>
                    <Checkbox
                      aria-label={item.removed_path}
                      checked={selected.has(item.id)}
                      disabled={item.state !== "trashed"}
                      onChange={() => toggle(item.id)}
                    />
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{pathName(item.removed_path)}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{pathName(item.kept_path)}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge variant="light">{t(KIND_LABEL[item.kind])}</Badge>
                  </Table.Td>
                  <Table.Td>{formatFileSize(item.size_bytes)}</Table.Td>
                  <Table.Td>
                    <Badge color={STATE_COLOR[item.state]} variant="light">
                      {t(STATE_LABEL[item.state])}
                    </Badge>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">
                      {formatRelativeTime(item.created_at, i18n.language, t("wash.justNow"))}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">
                      {expiryLabel(item, (value) =>
                        formatRelativeTime(value, i18n.language, t("wash.justNow")),
                      ) ?? t("wash.never")}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
      <ListPagination totalPages={totalPages} page={page} onChange={changePage} />
    </Stack>
  );
}

/** 到期时间只在还能被清理时有意义: 已清理的记录不再有倒计时. */
function expiryLabel(item: WashRecordItem, format: (value: string) => string): string | null {
  if (item.state === "purged") return null;
  if (!item.expires_at) return null;
  return format(item.expires_at);
}
