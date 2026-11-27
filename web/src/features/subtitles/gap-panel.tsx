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
import { IconAlertTriangle, IconRefresh } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  listTasksQueryKey,
  submitTaskMutation,
  subtitleGapsOptions,
  subtitleGapsQueryKey,
  subtitleStatusQueryKey,
} from "@/client/@tanstack/react-query.gen";
import type { SubtitleGapItem } from "@/client/types.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { formatDateTime } from "@/lib/utils";

/** 一次最多列出多少份: 缺口通常只有几十份, 200 足够翻完; 后端上限 2000. */
const GAP_LIMIT = 200;

const KIND_COLOR: Record<SubtitleGapItem["kind"], string> = {
  missing: "blue",
  pointer: "grape",
  multi_disc: "gray",
};

const REASON_COLOR: Record<SubtitleGapItem["reason"], string> = {
  fetch_failed: "red",
  pointer: "grape",
  multi_disc: "gray",
  fetch_empty: "yellow",
  not_attempted: "blue",
};

/**
 * 缺字幕清单: 哪个番号缺、为什么、试过几次、能不能重试.
 *
 * 原因与次数都来自任务历史, 因此「网盘直链所以转写跳过」这类结论在这张表里就能看见,
 * 不用再去翻日志 —— 这是用户最初提的问题.
 */
export function SubtitleGapPanel() {
  const { t, i18n } = useTranslation(["ops", "common"]);
  const queryClient = useQueryClient();
  const gapsQuery = useQuery(subtitleGapsOptions({ query: { limit: GAP_LIMIT } }));
  const data = gapsQuery.data;

  const retry = useMutation({
    ...submitTaskMutation(),
    onSuccess: () => {
      notifications.show({ message: t("ops:subtitles.retryStarted"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: subtitleStatusQueryKey() });
      void queryClient.invalidateQueries({ queryKey: listTasksQueryKey() });
      void queryClient.invalidateQueries({ queryKey: subtitleGapsQueryKey() });
    },
    onError: (error) => {
      notifications.show({
        message: extractErrorMessage(error, t("ops:subtitles.gapsFailed")),
        color: "red",
      });
    },
  });

  // 逐条提交而不是并发: 每份都会起一个任务, 一次几十个并发没有意义还容易撞站点限速.
  const retrySequentially = async (items: SubtitleGapItem[]) => {
    for (const item of items) {
      await retry.mutateAsync({
        body: { type: "subtitle_fetch", media_file_id: item.media_file_id },
      });
    }
  };

  const retryOne = (item: SubtitleGapItem) => {
    retry.mutate({ body: { type: "subtitle_fetch", media_file_id: item.media_file_id } });
  };

  if (gapsQuery.isPending) {
    return (
      <Center py="md">
        <Loader size="sm" />
      </Center>
    );
  }
  if (gapsQuery.isError) {
    return (
      <Alert
        color="red"
        icon={<IconAlertTriangle size={16} />}
        title={t("ops:subtitles.gapsFailed")}
      >
        {extractErrorMessage(gapsQuery.error, t("ops:subtitles.gapsFailed"))}
      </Alert>
    );
  }
  if (data === undefined || data.total === 0) {
    return (
      <Stack gap="xs">
        <Title order={4}>{t("ops:subtitles.gapsTitle")}</Title>
        <Text c="dimmed" size="sm">
          {t("ops:subtitles.gapsEmpty")}
        </Text>
      </Stack>
    );
  }

  const retryable = data.items.filter((item) => item.retryable && item.reason !== "multi_disc");

  return (
    <Stack gap="xs">
      <Group justify="space-between" wrap="wrap">
        <Group gap="xs">
          <Title order={4}>{t("ops:subtitles.gapsTitle")}</Title>
          <Badge color="gray" variant="light">
            {t("ops:subtitles.gapsTotal", { total: data.total })}
          </Badge>
          {(data.counts.pointer ?? 0) > 0 ? (
            <Badge color="grape" variant="light">
              {t("ops:subtitles.gapsPointer", { count: data.counts.pointer ?? 0 })}
            </Badge>
          ) : null}
          {(data.counts.failed ?? 0) > 0 ? (
            <Badge color="red" variant="light">
              {t("ops:subtitles.gapsFailedCount", { count: data.counts.failed ?? 0 })}
            </Badge>
          ) : null}
        </Group>
        <Button
          variant="subtle"
          size="xs"
          leftSection={<IconRefresh size={14} />}
          disabled={retryable.length === 0 || retry.isPending}
          loading={retry.isPending}
          onClick={() => void retrySequentially(retryable)}
        >
          {t("ops:subtitles.retryAll", { count: retryable.length })}
        </Button>
      </Group>
      <Text c="dimmed" size="xs">
        {t("ops:subtitles.gapsHint", { days: data.window_days })}
      </Text>
      <Table.ScrollContainer minWidth={720}>
        <Table striped withTableBorder highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("ops:subtitles.gapNumber")}</Table.Th>
              <Table.Th>{t("ops:subtitles.gapKind")}</Table.Th>
              <Table.Th>{t("ops:subtitles.gapReason")}</Table.Th>
              <Table.Th>{t("ops:subtitles.gapAttempts")}</Table.Th>
              <Table.Th>{t("ops:subtitles.gapLastAttempt")}</Table.Th>
              <Table.Th>{t("ops:subtitles.gapAction")}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data.items.map((item) => (
              <Table.Tr key={item.media_file_id}>
                <Table.Td>
                  <Text size="sm" ff="monospace">
                    {item.number ?? "—"}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Badge color={KIND_COLOR[item.kind]} variant="light" size="sm">
                    {t(`ops:subtitles.gapKindOption.${item.kind}`)}
                  </Badge>
                </Table.Td>
                <Table.Td>
                  <Stack gap={2}>
                    <Group gap="xs">
                      <Badge color={REASON_COLOR[item.reason]} variant="light" size="sm">
                        {t(`ops:subtitles.gapReasonOption.${item.reason}`)}
                      </Badge>
                    </Group>
                    <Text size="xs" c="dimmed">
                      {item.detail}
                    </Text>
                  </Stack>
                </Table.Td>
                <Table.Td>
                  {(item.attempts ?? 0) === 0 ? (
                    <Text size="sm" c="dimmed">
                      —
                    </Text>
                  ) : (
                    <Text size="sm" c={(item.failed ?? 0) > 0 ? "red" : undefined}>
                      {(item.failed ?? 0) > 0
                        ? t("ops:subtitles.gapAttemptsFailed", {
                            attempts: item.attempts,
                            failed: item.failed,
                          })
                        : t("ops:subtitles.gapAttemptsOnly", { attempts: item.attempts ?? 0 })}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <Text size="sm" c="dimmed">
                    {item.last_attempt_at === null
                      ? "—"
                      : formatDateTime(item.last_attempt_at, i18n.language)}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Button
                    variant="light"
                    size="compact-xs"
                    disabled={!item.retryable || retry.isPending}
                    onClick={() => retryOne(item)}
                  >
                    {t("ops:subtitles.retryOne")}
                  </Button>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {data.total > data.items.length ? (
        <Text c="dimmed" size="xs">
          {t("ops:subtitles.gapsTruncated", { shown: data.items.length, total: data.total })}
        </Text>
      ) : null}
    </Stack>
  );
}
