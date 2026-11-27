import { Badge, Card, Group, Progress, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { listTasksOptions } from "@/client/@tanstack/react-query.gen";
import type { TaskListItem } from "@/client/types.gen";
import { useProgressStore } from "@/stores/progress";

/** 与状态卡同频: 任务进度落库后由这个间隔拉回来, WS 事件到达时再覆盖一次. */
const STATUS_POLL_MS = 5000;
const ACTIVE_LIMIT = 20;

/** 字幕页会入队的三种任务; 补全与转写各是一种「补全方式」, 巡检只统计. */
const SUBTITLE_TASK_TYPES = ["subtitle_fetch", "subtitle_audit", "subtitle_generate"] as const;

const METHOD_LABEL = {
  subtitle_fetch: "ops:subtitles.methodFetch",
  subtitle_generate: "ops:subtitles.methodGenerate",
  subtitle_audit: "ops:subtitles.methodAudit",
} as const;

const METHOD_COLOR: Record<string, string> = {
  subtitle_fetch: "blue",
  subtitle_generate: "grape",
  subtitle_audit: "gray",
};

/**
 * 进行中的字幕任务与实时进度.
 *
 * 进度有两个来源: 后端落库的快照 (刷新页面后仍在, 但最多晚一个轮询间隔) 与 WS 的
 * `task.progress` 事件 (实时, 只到打开着的页面). 这里先取落库值再用 WS 值覆盖.
 */
export function SubtitleProgressPanel() {
  const { t } = useTranslation(["ops", "tasks", "common"]);
  const progressByTask = useProgressStore((s) => s.byTask);
  const activeQuery = useQuery({
    ...listTasksOptions({
      query: {
        type: [...SUBTITLE_TASK_TYPES],
        status: ["running", "queued"],
        roots_only: false,
        limit: ACTIVE_LIMIT,
        sort_by: "created_at",
        order: "asc",
      },
    }),
    refetchInterval: STATUS_POLL_MS,
  });

  const items = activeQuery.data?.items ?? [];

  return (
    <Card withBorder padding="md">
      <Stack gap="sm">
        <Group justify="space-between">
          <Text fw={600}>{t("ops:subtitles.progressTitle")}</Text>
          <Badge variant="light" color={items.length > 0 ? "blue" : "gray"}>
            {t("ops:subtitles.progressCount", { total: items.length })}
          </Badge>
        </Group>
        {items.length === 0 ? (
          <Text size="sm" c="dimmed">
            {t("ops:subtitles.progressEmpty")}
          </Text>
        ) : (
          items.map((task) => (
            <ProgressRow key={task.id} task={task} live={progressByTask[task.id]} />
          ))
        )}
      </Stack>
    </Card>
  );
}

interface ProgressRowProps {
  task: TaskListItem;
  live?: { current: number; total: number; message: string };
}

function ProgressRow({ task, live }: ProgressRowProps) {
  const { t } = useTranslation(["ops", "tasks"]);
  const current = live?.current ?? task.progress_current ?? 0;
  const total = live?.total ?? task.progress_total ?? 0;
  const message = live?.message || task.progress_message || "";
  const percent = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
  // 标题缺省时按任务范围兜底: 全库任务没有番号也没有库名.
  const scope = task.title ?? t("tasks:scope.allLibraries");

  return (
    <Stack gap={4}>
      <Group gap="xs" wrap="wrap">
        <Badge size="sm" variant="light" color={METHOD_COLOR[task.type] ?? "gray"}>
          {t(METHOD_LABEL[task.type as keyof typeof METHOD_LABEL] ?? "ops:subtitles.methodAudit")}
        </Badge>
        <Text size="sm" fw={500}>
          {scope}
        </Text>
        <Badge size="xs" variant="light" color={task.status === "running" ? "teal" : "gray"}>
          {t(`tasks:status.${task.status}`)}
        </Badge>
        <Text size="xs" c="dimmed">
          {t("ops:subtitles.progressNumbers", { current, total: total > 0 ? total : "?" })}
        </Text>
      </Group>
      <Progress value={percent} size="sm" animated={task.status === "running"} />
      {message ? (
        <Text size="xs" c="dimmed">
          {message}
        </Text>
      ) : null}
    </Stack>
  );
}
