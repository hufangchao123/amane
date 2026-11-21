import { Badge, Card, Center, Loader, Stack, Table, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { listTasksOptions } from "@/client/@tanstack/react-query.gen";
import type { TaskResponse } from "@/client/types.gen";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { statusColor } from "@/lib/task/display";

/** 最近几轮补全足够判断"点了有没有效果"; 更早的记录去任务页看. */
const RECENT_LIMIT = 5;
const STATUS_POLL_MS = 5000;

function subtitleCounts(
  task: TaskResponse,
): { fetched: number; missing: number; failed: number } | null {
  const result = task.result;
  if (!result || result.type !== "subtitle_fetch") return null;
  return { fetched: result.fetched ?? 0, missing: result.missing ?? 0, failed: result.failed ?? 0 };
}

/**
 * 最近的字幕补全任务.
 *
 * 只显示计数不显示逐条来源报错: 明细在任务详情页, 这里回答的是"上一轮补了几份".
 */
export function SubtitleRecentRuns() {
  const { t, i18n } = useTranslation(["ops", "tasks"]);
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
        {tasksQuery.isLoading ? (
          <Center py="md">
            <Loader size="sm" />
          </Center>
        ) : items.length === 0 ? (
          <Text size="sm" c="dimmed">
            {t("ops:subtitles.recentEmpty")}
          </Text>
        ) : (
          <Table.ScrollContainer minWidth={480}>
            <Table verticalSpacing="xs">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("ops:subtitles.recentStatus")}</Table.Th>
                  <Table.Th>{t("ops:subtitles.recentResult")}</Table.Th>
                  <Table.Th>{t("ops:subtitles.recentTime")}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {items.map((task) => {
                  const counts = subtitleCounts(task);
                  return (
                    <Table.Tr key={task.id}>
                      <Table.Td>
                        <Badge size="sm" variant="light" color={statusColor(task.status)}>
                          {t(`tasks:status.${task.status}`)}
                        </Badge>
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm">
                          {counts
                            ? t("ops:subtitles.recentCounts", {
                                fetched: counts.fetched,
                                missing: counts.missing,
                                failed: counts.failed,
                              })
                            : (task.error ?? "-")}
                        </Text>
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
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </Stack>
    </Card>
  );
}
