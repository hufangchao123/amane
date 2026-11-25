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
import { IconAlertCircle, IconDeviceFloppy } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  listLibrariesOptions,
  submitTaskMutation,
  washCandidatesOptions,
  washCandidatesQueryKey,
  washChoiceClearMutation,
  washChoiceSetMutation,
  washStatusQueryKey,
} from "@/client/@tanstack/react-query.gen";
import type { WashCandidate, WashCandidateSortField, WashRecordKind } from "@/client/types.gen";
import { ListPagination } from "@/components/common/list-pagination";
import { PageSizeSelect } from "@/components/common/page-size-select";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { formatFileSize } from "@/lib/utils";
import { useUIStore } from "@/stores/ui";
import { groupNumber, pathName } from "./path-name";
import { pickOption } from "./pick-option";

/** 与 `WashCandidateListResponse` 的 kind 查询参数同形; 生成物里该别名被内联, 这里补一处. */
type WashScope = "local" | "strm" | "all";

const KIND_COLOR: Record<WashRecordKind, string> = { local: "blue", strm: "grape" };
const KIND_LABEL: Record<WashRecordKind, "wash.kind.local" | "wash.kind.strm"> = {
  local: "wash.kind.local",
  strm: "wash.kind.strm",
};

const SCOPE_OPTIONS = ["all", "local", "strm"] as const satisfies readonly WashScope[];
const SORT_OPTIONS = [
  "bytes_freed",
  "group_key",
  "keep_path",
] as const satisfies readonly WashCandidateSortField[];

/**
 * 勾选用的键: 番号相同而类型不同的两组是两份互不相干的分组, 只用 group_key 会让它们联动.
 * 提交时再按 group_key 拆回去, 因为任务只认番号.
 */
function selectionKey(item: WashCandidate): string {
  return `${item.kind}:${item.group_key}`;
}

/** 候选分组表: 显示算法给出的保留者与落选者, 并把使用者的显式决定写回 wash_choices.
 *
 * 三个决定 (留这份 / 都留着 / 恢复) 只改数据库里的选择, 不搬文件 —— 搬盘仍由「执行」入队的任务负责.
 * 决定会改变保留者与可释放体积, 所以成功后让候选与状态卡一起重取, 表格据此重排;
 * 不弹成功提示, 重排后的表格与徽章本身就是反馈, 再弹一次只是噪音.
 * 被忽略的组留在表里且「可释放」为 0: 使用者要能看见自己留了哪些组, 也要能当场撤销.
 * 钉住的组同理 —— 撤销钉住与忽略同一个端点, 区别只在当前决定是哪种.
 *
 * 勾选分组后「执行选中组」只把这些组交给任务 (group_keys), 不必重算全部候选;
 * 选择按「类型 + 番号」记录, 提交时只取番号 —— 任务按番号定位分组, 而同一番号在两类库里是两组.
 * 已忽略的组不可勾选: 任务会跳过它们, 勾上只会让计数与执行结果对不上.
 */
export function WashCandidates() {
  const { t } = useTranslation(["ops", "common"]);
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [scope, setScope] = useState<WashScope>("all");
  const [libraryId, setLibraryId] = useState<number | null>(null);
  const [sortBy, setSortBy] = useState<WashCandidateSortField>("bytes_freed");
  const [selectedKeys, setSelectedKeys] = useState<ReadonlySet<string>>(new Set());
  const pageSize = useUIStore((s) => s.pageSizes.washCandidates);
  const librariesQuery = useQuery(listLibrariesOptions());
  const query = useQuery(
    washCandidatesOptions({
      query: {
        kind: scope,
        library_id: libraryId ?? undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
        sort_by: sortBy,
        order: "desc",
      },
    }),
  );
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  // 已忽略的组不参与勾选: 任务会跳过它们, 勾上只会让计数与执行结果对不上.
  const selectable = items.filter((item) => item.ignored !== true);
  const selectedItems = selectable.filter((item) => selectedKeys.has(selectionKey(item)));
  const selectedCount = selectedItems.length;
  const allSelected =
    selectable.length > 0 && selectable.every((item) => selectedKeys.has(selectionKey(item)));

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: washCandidatesQueryKey() });
    void queryClient.invalidateQueries({ queryKey: washStatusQueryKey() });
  };
  const onChooseError = (err: unknown) =>
    notifications.show({
      message: extractErrorMessage(err, t("wash.choiceFailed")),
      color: "red",
    });

  const keep = useMutation({
    ...washChoiceSetMutation(),
    onSuccess: invalidate,
    onError: onChooseError,
  });
  const ignore = useMutation({
    ...washChoiceSetMutation(),
    onSuccess: invalidate,
    onError: onChooseError,
  });
  const restore = useMutation({
    ...washChoiceClearMutation(),
    onSuccess: invalidate,
    onError: onChooseError,
  });
  const apply = useMutation({
    ...submitTaskMutation(),
    onSuccess: () => {
      notifications.show({ message: t("wash.applyStarted"), color: "blue" });
      setSelectedKeys(new Set());
      invalidate();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  // 三个决定都在同一列按钮上排队, 任一个在飞时锁住全部, 免得对同一组发出互相覆盖的请求.
  const busy = keep.isPending || ignore.isPending || restore.isPending;
  const pendingKeepPath = keep.isPending ? keep.variables?.body.keep_path : undefined;
  const pendingIgnoreKey = ignore.isPending ? ignore.variables?.body.group_key : undefined;
  const pendingRestoreKey = restore.isPending ? restore.variables?.path.group_key : undefined;

  function changeFilter(next: () => void) {
    next();
    setPage(1);
    // 筛选条件变了, 勾选的分组可能已不在表里: 一并清掉, 免得执行到看不见的组.
    setSelectedKeys(new Set());
  }

  function toggleGroup(key: string) {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function togglePage() {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (allSelected) for (const item of selectable) next.delete(selectionKey(item));
      else for (const item of selectable) next.add(selectionKey(item));
      return next;
    });
  }

  async function startApplySelected() {
    const ok = await confirm({
      title: t("wash.confirmApplySelectedTitle"),
      message: t("wash.confirmApplySelectedMessage", { count: selectedCount }),
      confirmLabel: t("wash.applySelected"),
    });
    if (!ok) return;
    apply.mutate({
      body: {
        type: "wash_apply",
        // 勾选按「类型 + 番号」记, 任务只认番号; 两类库里同番号的两组去重后各执行一次.
        group_keys: [...new Set(selectedItems.map((item) => item.group_key))],
        kind: scope,
        library_id: libraryId,
      },
    });
  }

  return (
    <Stack gap="xs">
      <Group justify="space-between" wrap="wrap">
        <Title order={4}>{t("wash.candidateTitle")}</Title>
        <Group gap="xs" wrap="wrap">
          <Select
            aria-label={t("wash.filterKind")}
            data={SCOPE_OPTIONS.map((value) => ({
              value,
              label: value === "all" ? t("wash.kindAll") : t(KIND_LABEL[value]),
            }))}
            value={scope}
            onChange={(value) =>
              changeFilter(() => setScope(pickOption(SCOPE_OPTIONS, value, "all")))
            }
            allowDeselect={false}
            w={140}
          />
          <Select
            aria-label={t("wash.filterLibrary")}
            placeholder={t("wash.libraryAll")}
            data={(librariesQuery.data?.items ?? []).map((library) => ({
              value: String(library.id),
              label: library.name,
            }))}
            value={libraryId === null ? null : String(libraryId)}
            onChange={(value) =>
              changeFilter(() => setLibraryId(value === null ? null : Number(value)))
            }
            clearable
            w={200}
          />
          <Select
            aria-label={t("wash.sortBy")}
            data={SORT_OPTIONS.map((value) => ({
              value,
              label:
                value === "group_key"
                  ? t("wash.sort.groupKey")
                  : value === "keep_path"
                    ? t("wash.sort.keepPath")
                    : t("wash.sort.bytesFreed"),
            }))}
            value={sortBy}
            onChange={(value) =>
              changeFilter(() => setSortBy(pickOption(SORT_OPTIONS, value, "bytes_freed")))
            }
            allowDeselect={false}
            w={180}
          />
          <PageSizeSelect
            sizeKey="washCandidates"
            onChanged={() => {
              setPage(1);
              setSelectedKeys(new Set());
            }}
          />
        </Group>
      </Group>
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Text size="sm" c="dimmed">
          {t("wash.selectedGroups", { count: selectedCount })}
        </Text>
        <Button
          size="xs"
          color="orange"
          leftSection={<IconDeviceFloppy size={14} />}
          disabled={selectedCount === 0}
          loading={apply.isPending}
          onClick={() => void startApplySelected()}
        >
          {t("wash.applySelected")}
        </Button>
      </Group>
      {query.isError ? (
        <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />}>
          <Text size="sm">{t("wash.candidatesLoadFailed")}</Text>
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
          {t("wash.candidateEmpty")}
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={920}>
          <Table highlightOnHover verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={36}>
                  <Checkbox
                    aria-label={t("wash.column.group")}
                    checked={allSelected}
                    indeterminate={
                      !allSelected &&
                      selectable.some((item) => selectedKeys.has(selectionKey(item)))
                    }
                    disabled={selectable.length === 0}
                    onChange={togglePage}
                  />
                </Table.Th>
                <Table.Th>{t("wash.column.group")}</Table.Th>
                <Table.Th>{t("wash.column.kind")}</Table.Th>
                <Table.Th>{t("wash.column.keep")}</Table.Th>
                <Table.Th>{t("wash.column.drops")}</Table.Th>
                <Table.Th>{t("wash.column.bytes")}</Table.Th>
                <Table.Th>{t("wash.column.actions")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((item) => {
                const drops = item.drop_paths ?? [];
                const ignored = item.ignored === true;
                const pinned = item.pinned === true;
                return (
                  <Table.Tr key={`${item.kind}:${item.group_key}`}>
                    <Table.Td>
                      <Checkbox
                        aria-label={groupNumber(item.group_key)}
                        checked={selectedKeys.has(selectionKey(item))}
                        disabled={ignored}
                        onChange={() => toggleGroup(selectionKey(item))}
                      />
                    </Table.Td>
                    <Table.Td>
                      <Group gap={6} wrap="nowrap">
                        <Text size="sm">{groupNumber(item.group_key)}</Text>
                        {ignored ? (
                          <Badge size="xs" color="gray" variant="light">
                            {t("wash.ignored")}
                          </Badge>
                        ) : null}
                      </Group>
                    </Table.Td>
                    <Table.Td>
                      <Badge color={KIND_COLOR[item.kind]} variant="light">
                        {t(KIND_LABEL[item.kind])}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <Group gap={6} wrap="nowrap">
                        <Text size="sm" c={ignored ? "dimmed" : undefined}>
                          {pathName(item.keep_path)}
                        </Text>
                        {pinned ? (
                          <Badge size="xs" color="teal" variant="light">
                            {t("wash.pinned")}
                          </Badge>
                        ) : null}
                      </Group>
                      <Text size="xs" c="dimmed">
                        {formatFileSize(item.keep_size ?? 0)}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      {ignored ? (
                        // 整组都留着: 没有落选者, 也就不给「留这份」, 只说明现状.
                        <Text size="xs" c="dimmed">
                          {t("wash.ignoredDrops")}
                        </Text>
                      ) : (
                        <Stack gap={4}>
                          {drops.map((path) => (
                            <Group key={path} gap="xs" justify="space-between" wrap="nowrap">
                              <Text size="xs" c="dimmed" title={path}>
                                {pathName(path)}
                              </Text>
                              <Button
                                size="compact-xs"
                                variant="light"
                                loading={pendingKeepPath === path}
                                disabled={busy}
                                onClick={() =>
                                  keep.mutate({
                                    body: {
                                      group_key: item.group_key,
                                      action: "keep",
                                      keep_path: path,
                                    },
                                  })
                                }
                              >
                                {t("wash.keepThis")}
                              </Button>
                            </Group>
                          ))}
                        </Stack>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" c={ignored ? "dimmed" : undefined}>
                        {formatFileSize(item.bytes_freed ?? 0)}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      {ignored ? (
                        <Button
                          size="compact-xs"
                          variant="default"
                          loading={pendingRestoreKey === item.group_key}
                          disabled={busy}
                          onClick={() => restore.mutate({ path: { group_key: item.group_key } })}
                        >
                          {t("wash.choiceRestore")}
                        </Button>
                      ) : pinned ? (
                        // 钉住是显式选择的结果, 撤销与忽略同一个端点.
                        <Button
                          size="compact-xs"
                          variant="default"
                          loading={pendingRestoreKey === item.group_key}
                          disabled={busy}
                          onClick={() => restore.mutate({ path: { group_key: item.group_key } })}
                        >
                          {t("wash.unpin")}
                        </Button>
                      ) : (
                        <Button
                          size="compact-xs"
                          variant="light"
                          color="gray"
                          loading={pendingIgnoreKey === item.group_key}
                          disabled={busy}
                          onClick={() =>
                            ignore.mutate({ body: { group_key: item.group_key, action: "ignore" } })
                          }
                        >
                          {t("wash.keepAll")}
                        </Button>
                      )}
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
      <ListPagination
        totalPages={totalPages}
        page={page}
        onChange={(next) => {
          setPage(next);
          // 跨页保留勾选会执行到看不见的组, 翻页即清空.
          setSelectedKeys(new Set());
        }}
      />
    </Stack>
  );
}
