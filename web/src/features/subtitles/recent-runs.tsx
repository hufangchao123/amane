import { Alert, Badge, Card, Center, Group, Loader, Stack, Table, Text } from "@mantine/core";
import { IconAlertCircle } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { getTaskOptions, listTasksOptions } from "@/client/@tanstack/react-query.gen";
import type { TaskListItem } from "@/client/types.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { formatRelativeTime } from "@/lib/format-relative-time";
import type { TaskResult } from "@/lib/task/display";
import { statusColor } from "@/lib/task/display";

/** 最近几轮足够判断"点了有没有效果"; 更早的记录去任务页看.
 *
 * 补全、巡检、转写共用一张表, 因此每类留的条数要宽裕一点: 五条里若挤进两类别的行, 任何一类都可能只剩一条.
 */
const RECENT_LIMIT = 8;
const STATUS_POLL_MS = 5000;

/** 字幕页会入队的三种任务; 顺序即表格里三类行的相对权重. */
const SUBTITLE_TASK_TYPES = ["subtitle_fetch", "subtitle_audit", "subtitle_generate"] as const;

/**
 * 三类任务各自的计数各占一格.
 *
 * 只报一个合计是不够的: "缺口"是来源确实没有这份字幕, "失败"是这一轮没办成 (缺元数据、
 * 落位冲突、来源限流), 后者下一轮还会再试. 两种都混在一格里, 用户没法判断要不要重跑.
 * 巡检与转写的结果字段不同, 因此按类型分开渲染, 不硬凑成一列数字.
 */
export function SubtitleRecentRuns() {
  const { t } = useTranslation(["ops", "tasks", "common"]);
  const tasksQuery = useQuery({
    ...listTasksOptions({
      query: {
        type: [...SUBTITLE_TASK_TYPES],
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
          <Table.ScrollContainer minWidth={620}>
            <Table verticalSpacing="xs">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("ops:subtitles.recentKind")}</Table.Th>
                  <Table.Th>{t("ops:subtitles.recentStatus")}</Table.Th>
                  <Table.Th>{t("ops:subtitles.recentResult")}</Table.Th>
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
 * 结果格在开合间保持同一行高: 取详情时占位, 失败时给出原因, 表格不因加载而抖动.
 */
function RecentRunRow({ task }: { task: TaskListItem }) {
  const { t, i18n } = useTranslation(["ops", "tasks", "common"]);
  const finished = task.status === "done" || task.status === "failed";
  const detailQuery = useQuery({
    ...getTaskOptions({ path: { task_id: task.id } }),
    enabled: finished,
  });
  const result = detailQuery.data?.result ?? undefined;

  return (
    <Table.Tr>
      <Table.Td>
        <Text size="sm">{t(`tasks:filters.${task.type}`)}</Text>
      </Table.Td>
      <Table.Td>
        <Badge size="sm" variant="light" color={statusColor(task.status)}>
          {t(`tasks:status.${task.status}`)}
        </Badge>
      </Table.Td>
      <Table.Td>
        {!finished ? (
          <Text size="sm" c="dimmed">
            -
          </Text>
        ) : detailQuery.isPending ? (
          <Text size="sm" c="dimmed">
            …
          </Text>
        ) : detailQuery.isError ? (
          <Text size="sm" c="red">
            {extractErrorMessage(detailQuery.error, t("common:toast.operationFailed"))}
          </Text>
        ) : result ? (
          <RunResult result={result} />
        ) : (
          <Text size="sm" c="dimmed">
            {task.error ?? "-"}
          </Text>
        )}
      </Table.Td>
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

/** 一份结果里值得看的那几个数, 按任务类型给出. */
function RunResult({ result }: { result: TaskResult }) {
  const { t } = useTranslation("ops");
  switch (result.type) {
    case "subtitle_fetch":
      return (
        <Group gap="sm" wrap="nowrap">
          <Metric label={t("subtitles.recentFetched")} value={result.fetched ?? 0} tone="good" />
          <Metric label={t("subtitles.recentMissing")} value={result.missing ?? 0} />
          <Metric label={t("subtitles.recentFailed")} value={result.failed ?? 0} tone="bad" />
        </Group>
      );
    case "subtitle_generate":
      return (
        <Group gap="sm" wrap="nowrap">
          <Metric
            label={t("subtitles.recentGenerated")}
            value={result.generated ?? 0}
            tone="good"
          />
          <Metric label={t("subtitles.recentTranslated")} value={result.translated ?? 0} />
          <Metric label={t("subtitles.recentSkipped")} value={result.skipped ?? 0} />
          <Metric label={t("subtitles.recentFailed")} value={result.failed ?? 0} tone="bad" />
        </Group>
      );
    case "subtitle_audit":
      return (
        <Group gap="sm" wrap="nowrap">
          <Metric label={t("subtitles.recentVideos")} value={result.videos ?? 0} />
          <Metric label={t("subtitles.recentSubtitles")} value={result.subtitles ?? 0} />
          <Metric label={t("subtitles.recentMissing")} value={result.missing ?? 0} />
          <Metric label={t("subtitles.recentOrphans")} value={result.orphans ?? 0} />
        </Group>
      );
    default:
      return (
        <Text size="sm" c="dimmed">
          -
        </Text>
      );
  }
}

/** 一个计数: 标签用弱色, 数值用强色; 正负向的数值靠颜色区分, 不靠加字. */
function Metric({ label, value, tone }: { label: string; value: number; tone?: "good" | "bad" }) {
  const color = tone === "bad" ? "red" : tone === "good" && value > 0 ? "teal" : undefined;
  return (
    <Group gap={4} wrap="nowrap">
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text size="sm" fw={tone === "good" && value > 0 ? 600 : undefined} c={color}>
        {value}
      </Text>
    </Group>
  );
}
