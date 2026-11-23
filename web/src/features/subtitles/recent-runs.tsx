import { Alert, Badge, Card, Center, Loader, Stack, Table, Text } from "@mantine/core";
import { IconAlertCircle } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { getTaskOptions, listTasksOptions } from "@/client/@tanstack/react-query.gen";
import type { TaskListItem } from "@/client/types.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { formatRelativeTime } from "@/lib/format-relative-time";
import type { TaskResult } from "@/lib/task/display";
import { statusColor } from "@/lib/task/display";

/** 最近几轮补全足够判断"点了有没有效果"; 更早的记录去任务页看. */
const RECENT_LIMIT = 5;
const STATUS_POLL_MS = 5000;

interface SubtitleCounts {
  fetched: number;
  missing: number;
  failed: number;
}

function subtitleCounts(result: TaskResult | undefined): SubtitleCounts | null {
  if (!result || result.type !== "subtitle_fetch") return null;
  return { fetched: result.fetched ?? 0, missing: result.missing ?? 0, failed: result.failed ?? 0 };
}

/**
 * 一轮补全的三种结局各占一列.
 *
 * 只报一个合计是不够的: "缺口"是来源确实没有这份字幕, "失败"是这一轮没办成 (缺元数据、
 * 落位冲突、来源限流), 后者下一轮还会再试. 两种都混在一格里, 用户没法判断要不要重跑.
 */
export function SubtitleRecentRuns() {
  const { t } = useTranslation(["ops", "tasks", "common"]);
  const tasksQuery = useQuery({
    ...listTasksOptions({
      query: {
        type: ["subtitle_fetch"],
        limit: RECENT_LIMIT,
        sort_by: "created_at",
        order: "desc",
      },
    }),
    refetchInterval: STATUS_POLL_MS,
  });

  const items = tasksQuery.data?.items ?? [];

  return (
    <Card withBorder padding="md">
      <Stack gap="sm">
        <Text fw={600}>{t("ops:subtitles.recentTitle")}</Text>
        {tasksQuery.isError ? (
          <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />}>
            <Text size="sm">{t("ops:subtitles.recentLoadFailed")}</Text>
            <Text size="xs" c="dimmed">
              {extractErrorMessage(tasksQuery.error, t("common:toast.operationFailed"))}
            </Text>
          </Alert>
        ) : tasksQuery.isLoading ? (
          <Center py="md">
            <Loader size="sm" />
          </Center>
        ) : items.length === 0 ? (
          <Text size="sm" c="dimmed">
            {t("ops:subtitles.recentEmpty")}
          </Text>
        ) : (
          <Table.ScrollContainer minWidth={560}>
            <Table verticalSpacing="xs">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("ops:subtitles.recentStatus")}</Table.Th>
                  <Table.Th>{t("ops:subtitles.recentFetched")}</Table.Th>
                  <Table.Th>{t("ops:subtitles.recentMissing")}</Table.Th>
                  <Table.Th>{t("ops:subtitles.recentFailed")}</Table.Th>
                  <Table.Th>{t("ops:subtitles.recentTime")}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {items.map((task) => (
                  <RecentRunRow key={task.id} task={task} />
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
        <Text size="xs" c="dimmed">
          {t("ops:subtitles.recentLegend")}
        </Text>
      </Stack>
    </Card>
  );
}

/**
 * 列表项不带 result, 终态行按 id 取详情; 排队/运行中的行还没有结果可看.
 *
 * 三列计数在开合间保持同一行高: 取详情时占位, 失败时给出原因, 表格不因加载而抖动.
 */
function RecentRunRow({ task }: { task: TaskListItem }) {
  const { t, i18n } = useTranslation(["ops", "tasks", "common"]);
  const finished = task.status === "done" || task.status === "failed";
  const detailQuery = useQuery({
    ...getTaskOptions({ path: { task_id: task.id } }),
    enabled: finished,
  });
  const counts = subtitleCounts(detailQuery.data?.result ?? undefined);

  return (
    <Table.Tr>
      <Table.Td>
        <Badge size="sm" variant="light" color={statusColor(task.status)}>
          {t(`tasks:status.${task.status}`)}
        </Badge>
      </Table.Td>
      {counts ? (
        <>
          <Table.Td>
            <Text
              size="sm"
              fw={counts.fetched > 0 ? 600 : undefined}
              c={counts.fetched > 0 ? "teal" : "dimmed"}
            >
              {counts.fetched}
            </Text>
          </Table.Td>
          <Table.Td>
            <Text size="sm" c={counts.missing > 0 ? undefined : "dimmed"}>
              {counts.missing}
            </Text>
          </Table.Td>
          <Table.Td>
            <Text size="sm" c={counts.failed > 0 ? "red" : "dimmed"}>
              {counts.failed}
            </Text>
          </Table.Td>
        </>
      ) : (
        <Table.Td colSpan={3}>
          {finished && detailQuery.isPending ? (
            <Text size="sm" c="dimmed">
              …
            </Text>
          ) : detailQuery.isError ? (
            <Text size="sm" c="red">
              {extractErrorMessage(detailQuery.error, t("common:toast.operationFailed"))}
            </Text>
          ) : (
            <Text size="sm" c="dimmed">
              {task.error ?? "-"}
            </Text>
          )}
        </Table.Td>
      )}
      <Table.Td>
        <Text size="sm" c="dimmed">
          {formatRelativeTime(
            task.finished_at ?? task.created_at ?? "",
            i18n.language,
            t("ops:subtitles.justNow"),
          )}
        </Text>
      </Table.Td>
    </Table.Tr>
  );
}
