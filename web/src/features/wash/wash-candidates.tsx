import {
  Alert,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconAlertCircle } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  washCandidatesOptions,
  washCandidatesQueryKey,
  washChoiceClearMutation,
  washChoiceSetMutation,
  washStatusQueryKey,
} from "@/client/@tanstack/react-query.gen";
import type { WashRecordKind } from "@/client/types.gen";
import { ListPagination } from "@/components/common/list-pagination";
import { PageSizeSelect } from "@/components/common/page-size-select";
import { extractErrorMessage } from "@/lib/api-error";
import { formatFileSize } from "@/lib/utils";
import { useUIStore } from "@/stores/ui";
import { groupNumber, pathName } from "./path-name";

const KIND_COLOR: Record<WashRecordKind, string> = { local: "blue", strm: "grape" };
const KIND_LABEL: Record<WashRecordKind, "wash.kind.local" | "wash.kind.strm"> = {
  local: "wash.kind.local",
  strm: "wash.kind.strm",
};

/** 候选分组表: 显示算法给出的保留者与落选者, 并把使用者的显式决定写回 wash_choices.
 *
 * 三个决定 (留这份 / 都留着 / 恢复) 只改数据库里的选择, 不搬文件 —— 搬盘仍由「执行」入队的任务负责.
 * 决定会改变保留者与可释放体积, 所以成功后让候选与状态卡一起重取, 表格据此重排;
 * 不弹成功提示, 重排后的表格与徽章本身就是反馈, 再弹一次只是噪音.
 * 被忽略的组留在表里且「可释放」为 0: 使用者要能看见自己留了哪些组, 也要能当场撤销.
 */
export function WashCandidates() {
  const { t } = useTranslation(["ops", "common"]);
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const pageSize = useUIStore((s) => s.pageSizes.washCandidates);
  const query = useQuery(
    washCandidatesOptions({
      query: {
        limit: pageSize,
        offset: (page - 1) * pageSize,
        sort_by: "bytes_freed",
        order: "desc",
      },
    }),
  );
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

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

  // 三个决定都在同一列按钮上排队, 任一个在飞时锁住全部, 免得对同一组发出互相覆盖的请求.
  const busy = keep.isPending || ignore.isPending || restore.isPending;
  const pendingKeepPath = keep.isPending ? keep.variables?.body.keep_path : undefined;
  const pendingIgnoreKey = ignore.isPending ? ignore.variables?.body.group_key : undefined;
  const pendingRestoreKey = restore.isPending ? restore.variables?.path.group_key : undefined;

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <Title order={4}>{t("wash.candidateTitle")}</Title>
        <PageSizeSelect sizeKey="washCandidates" onChanged={() => setPage(1)} />
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
        <Table.ScrollContainer minWidth={880}>
          <Table highlightOnHover verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
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
      <ListPagination totalPages={totalPages} page={page} onChange={setPage} />
    </Stack>
  );
}
