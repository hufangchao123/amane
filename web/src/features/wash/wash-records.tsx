import {
  Alert,
  Badge,
  Button,
  Center,
  Checkbox,
  Group,
  Loader,
  Select,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconAlertCircle, IconRestore, IconTrash } from "@tabler/icons-react";
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
import { PageSizeSelect } from "@/components/common/page-size-select";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { formatFileSize } from "@/lib/utils";
import { useUIStore } from "@/stores/ui";
import { pathName } from "./path-name";
import { pickOptionOrNull } from "./pick-option";
import { useWashRefresh } from "./use-wash-refresh";

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
const STATE_OPTIONS = [
  "trashed",
  "restored",
  "purged",
] as const satisfies readonly WashRecordState[];
const KIND_OPTIONS = ["local", "strm"] as const satisfies readonly WashRecordKind[];

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
  const [stateFilter, setStateFilter] = useState<WashRecordState | null>(null);
  const [kindFilter, setKindFilter] = useState<WashRecordKind | null>(null);
  const pageSize = useUIStore((s) => s.pageSizes.washRecords);
  const query = useQuery(
    washRecordsOptions({
      query: {
        state: stateFilter ?? undefined,
        kind: kindFilter ?? undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
        sort_by: "created_at",
        order: "desc",
      },
    }),
  );
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const selectable = items.filter((item) => item.state === "trashed");
  const allSelected = selectable.length > 0 && selectable.every((item) => selected.has(item.id));
  // 勾选只发生在当前页, 因此按页内的行还原出选中项, 不必再为它们单独查一次.
  // 刷新后消失的记录 (别人还原了、定时任务清掉了) 由这里自然落选:
  // 提交已经不在库里的 id 会被后端整体拒绝 (422), 一条不合格就什么都不做.
  const selectedItems = items.filter((item) => selected.has(item.id));
  const selectedCount = selectedItems.length;

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

  function changeFilter(next: () => void) {
    next();
    setPage(1);
    // 筛选条件变了, 原来的选中可能已不在表里, 一并清空.
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

  async function startRestore() {
    const ok = await confirm({
      title: t("wash.confirmRestoreTitle"),
      message: t("wash.confirmRestoreMessage"),
      danger: false,
    });
    if (!ok) return;
    restore.mutate({ body: { ids: selectedItems.map((item) => item.id) } });
  }

  async function startPurge() {
    const due = selectedItems.filter(isPurgeable);
    if (due.length === 0) {
      // 任务只真删已过保留期的记录, 未到期的会全部计入跳过; 与其提交一条什么都不做的任务, 不如就地说明.
      notifications.show({
        message: t("wash.purgeNotDue", { count: selectedCount }),
        color: "yellow",
      });
      return;
    }
    const notDue = selectedCount - due.length;
    const ok = await confirm({
      title: t("wash.confirmPurgeTitle"),
      message:
        notDue > 0
          ? `${t("wash.confirmPurgeMessage")} ${t("wash.purgePartial", { count: notDue })}`
          : t("wash.confirmPurgeMessage"),
    });
    if (!ok) return;
    purge.mutate({ body: { ids: due.map((item) => item.id) } });
  }

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <Title order={4}>{t("wash.recordTitle")}</Title>
        <Group gap="xs">
          <PageSizeSelect
            sizeKey="washRecords"
            onChanged={() => {
              setPage(1);
              setSelected(new Set());
            }}
          />
          <Text size="sm" c="dimmed">
            {t("wash.selected", { count: selectedCount })}
          </Text>
          <Button
            size="xs"
            variant="default"
            leftSection={<IconRestore size={14} />}
            disabled={selectedCount === 0}
            loading={restore.isPending}
            onClick={() => void startRestore()}
          >
            {t("wash.restore")}
          </Button>
          <Button
            size="xs"
            color="red"
            leftSection={<IconTrash size={14} />}
            disabled={selectedCount === 0}
            loading={purge.isPending}
            onClick={() => void startPurge()}
          >
            {t("wash.purge")}
          </Button>
        </Group>
      </Group>
      <Group gap="xs" wrap="wrap">
        <Select
          aria-label={t("wash.column.state")}
          placeholder={t("wash.stateAll")}
          data={STATE_OPTIONS.map((value) => ({ value, label: t(STATE_LABEL[value]) }))}
          value={stateFilter}
          onChange={(value) =>
            changeFilter(() => setStateFilter(pickOptionOrNull(STATE_OPTIONS, value)))
          }
          clearable
          w={160}
        />
        <Select
          aria-label={t("wash.filterKind")}
          placeholder={t("wash.kindAll")}
          data={KIND_OPTIONS.map((value) => ({ value, label: t(KIND_LABEL[value]) }))}
          value={kindFilter}
          onChange={(value) =>
            changeFilter(() => setKindFilter(pickOptionOrNull(KIND_OPTIONS, value)))
          }
          clearable
          w={140}
        />
      </Group>
      {query.isError ? (
        <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />}>
          <Text size="sm">{t("wash.recordsLoadFailed")}</Text>
          <Text size="xs" c="dimmed">
            {extractErrorMessage(query.error, t("common:toast.operationFailed"))}
          </Text>
        </Alert>
      ) : query.isPending ? (
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

/** 与后端 ``wash_apply._is_purgeable`` 同一判据: 仍在回收站里, 且已过保留期.
 *
 * 没有到期时间的记录不动 (历史行), 这一点也照抄后端: 宁可留着, 也不猜它该不该删.
 */
function isPurgeable(item: WashRecordItem): boolean {
  if (item.state !== "trashed" || !item.expires_at) return false;
  const expires = Date.parse(item.expires_at);
  return !Number.isNaN(expires) && expires <= Date.now();
}

/** 到期时间只在还能被清理时有意义: 已清理的记录不再有倒计时. */
function expiryLabel(item: WashRecordItem, format: (value: string) => string): string | null {
  if (item.state === "purged") return null;
  if (!item.expires_at) return null;
  return format(item.expires_at);
}
