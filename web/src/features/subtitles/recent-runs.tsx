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

interface SubtitleCounts {
  fetched: number;
  missing: number;
  failed: number;
}

function subtitleCounts(task: TaskResponse): SubtitleCounts | null {
  const result = task.result;
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
                {items.map((task) => {
                  const counts = subtitleCounts(task);
                  return (
                    <Table.Tr key={task.id}>
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
                          <Text size="sm" c="dimmed">
                            {task.error ?? "-"}
                          </Text>
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
                })}
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
