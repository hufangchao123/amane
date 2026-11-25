import { ActionIcon, Alert, Badge, Card, Center, Group, Loader, Stack, Text } from "@mantine/core";
import { IconAlertCircle, IconRefresh } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { getTaskOptions, listTasksOptions } from "@/client/@tanstack/react-query.gen";
import type { TaskListItem } from "@/client/types.gen";
import { TaskResultPanel } from "@/components/task/task-result-panel";
import { extractErrorMessage } from "@/lib/api-error";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { statusColor } from "@/lib/task/display";

/** 维护页三个动作各自入队; 只弹「已入队」看不出这一轮究竟改了什么. */
const MAINTENANCE_TASK_TYPES = ["nfo_patch", "avatar_check", "upscale"] as const;
const RECENT_LIMIT = 5;
const POLL_MS = 5000;

/**
 * 最近几轮维护动作的结果.
 *
 * 取的是任务列表按类型过滤后的前几条, 列表项不带结果, 终态行再按 id 取详情;
 * 渲染交给任务详情页同一个 `TaskResultPanel`, 免得两处对同一份结果说法不一致.
 */
export function MaintenanceRecentResults() {
  const { t } = useTranslation("ops");
  const tasksQuery = useQuery({
    ...listTasksOptions({
      query: {
        type: [...MAINTENANCE_TASK_TYPES],
        limit: RECENT_LIMIT,
        sort_by: "created_at",
        order: "desc",
      },
    }),
    refetchInterval: POLL_MS,
  });

  const items = tasksQuery.data?.items ?? [];

  return (
    <Card withBorder padding="md">
      <Stack gap="sm">
        <Group justify="space-between">
          <Text fw={600}>{t("maintenance.recent.title")}</Text>
          <ActionIcon
            variant="subtle"
            loading={tasksQuery.isFetching}
            aria-label={t("maintenance.recent.refresh")}
            onClick={() => void tasksQuery.refetch()}
          >
            <IconRefresh size={16} />
          </ActionIcon>
        </Group>
        {tasksQuery.isError ? (
          <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />}>
            <Text size="sm">{t("maintenance.recent.loadFailed")}</Text>
            <Text size="xs" c="dimmed">
              {extractErrorMessage(tasksQuery.error, t("maintenance.recent.loadFailed"))}
            </Text>
          </Alert>
        ) : tasksQuery.isLoading ? (
          <Center py="md">
            <Loader size="sm" />
          </Center>
        ) : items.length === 0 ? (
          <Text size="sm" c="dimmed">
            {t("maintenance.recent.empty")}
          </Text>
        ) : (
          <Stack gap="sm">
            {items.map((task) => (
              <RecentResult key={task.id} task={task} />
            ))}
          </Stack>
        )}
      </Stack>
    </Card>
  );
}

function RecentResult({ task }: { task: TaskListItem }) {
  const { t, i18n } = useTranslation(["ops", "tasks"]);
  const finished = task.status === "done" || task.status === "failed";
  const detailQuery = useQuery({
    ...getTaskOptions({ path: { task_id: task.id } }),
    enabled: finished,
  });

  return (
    <Card withBorder padding="sm" bg="var(--mantine-color-default)">
      <Stack gap="xs">
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            <Text size="sm" fw={600}>
              {t(`tasks:filters.${task.type}`)}
            </Text>
            <Badge size="sm" variant="light" color={statusColor(task.status)}>
              {t(`tasks:status.${task.status}`)}
            </Badge>
          </Group>
          <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>
            {formatRelativeTime(
              task.finished_at ?? task.started_at ?? task.created_at ?? "",
              i18n.language,
              t("maintenance.recent.justNow"),
            )}
          </Text>
        </Group>
        {!finished ? (
          <Text size="xs" c="dimmed">
            {t("maintenance.recent.running")}
          </Text>
        ) : detailQuery.isPending ? (
          <Center py={4}>
            <Loader size="xs" />
          </Center>
        ) : detailQuery.isError ? (
          <Text size="xs" c="red">
            {extractErrorMessage(detailQuery.error, t("maintenance.recent.loadFailed"))}
          </Text>
        ) : (
          <TaskResultPanel
            result={detailQuery.data?.result ?? null}
            headline={task.error ?? null}
            failed={task.status === "failed"}
          />
        )}
      </Stack>
    </Card>
  );
}
