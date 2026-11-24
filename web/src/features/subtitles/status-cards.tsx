import { Badge, Button, Card, Group, Select, SimpleGrid, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconDownload, IconListSearch, IconMicrophone } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import {
  listLibrariesOptions,
  listTasksQueryKey,
  subtitleStatusOptions,
  subtitleStatusQueryKey,
  submitTaskMutation,
} from "@/client/@tanstack/react-query.gen";
import { extractErrorMessage } from "@/lib/api-error";

/** 排队中与运行中的任务不会自己通知前端, 因此状态卡按固定间隔重取, 任务结束后自然回落. */
const STATUS_POLL_MS = 5000;

interface StatCardProps {
  label: string;
  value: string;
  hint?: string;
}

function StatCard({ label, value, hint }: StatCardProps) {
  return (
    <Card withBorder padding="md">
      <Stack gap={4}>
        <Text size="xs" c="dimmed">
          {label}
        </Text>
        <Text fw={600} size="lg">
          {value}
        </Text>
        {hint ? (
          <Text size="xs" c="dimmed">
            {hint}
          </Text>
        ) : null}
      </Stack>
    </Card>
  );
}

/**
 * 状态卡与"补齐字幕"入口.
 *
 * 未启用时不隐藏按钮而是禁用并给出原因: 用户看到的第一反应是"为什么点不了", 文案要能直接回答.
 */
export function SubtitleStatusCards() {
  const { t } = useTranslation(["ops", "common"]);
  const queryClient = useQueryClient();
  const [libraryId, setLibraryId] = useState<number | null>(null);
  const librariesQuery = useQuery(listLibrariesOptions());
  const statusQuery = useQuery({
    ...subtitleStatusOptions(),
    refetchInterval: STATUS_POLL_MS,
  });

  const submit = useMutation({
    ...submitTaskMutation(),
    onSuccess: () => {
      notifications.show({ message: t("ops:subtitles.fetchStarted"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: subtitleStatusQueryKey() });
      void queryClient.invalidateQueries({ queryKey: listTasksQueryKey() });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  /** 巡检只读, 与抓取分开: 抓取的按钮受 enabled 约束, 巡检不需要. */
  const audit = useMutation({
    ...submitTaskMutation(),
    onSuccess: () => {
      notifications.show({ message: t("ops:subtitles.auditStarted"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: listTasksQueryKey() });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  /** 转写生成: 一份要跑几十分钟, 只对选中的库发起, 单轮份数由后端默认值兜住. */
  const generate = useMutation({
    ...submitTaskMutation(),
    onSuccess: () => {
      notifications.show({ message: t("ops:subtitles.generateStarted"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: subtitleStatusQueryKey() });
      void queryClient.invalidateQueries({ queryKey: listTasksQueryKey() });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  const status = statusQuery.data;
  const enabled = status?.enabled ?? false;
  const running = submit.isPending;

  return (
    <Stack gap="md">
      <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
        <StatCard
          label={t("ops:subtitles.statusLabel")}
          value={enabled ? t("ops:subtitles.enabled") : t("ops:subtitles.disabled")}
          hint={enabled ? undefined : t("ops:subtitles.disabledHint")}
        />
        <StatCard
          label={t("ops:subtitles.missingLabel")}
          value={status ? String(status.missing) : "-"}
          hint={
            status && status.multi_disc
              ? `${t("ops:subtitles.missingHint")} · ${t("ops:subtitles.multiDiscHint", { parts: status.multi_disc })}`
              : t("ops:subtitles.missingHint")
          }
        />
        <StatCard
          label={t("ops:subtitles.queuedLabel")}
          value={status ? String(status.queued) : "-"}
        />
      </SimpleGrid>
      <Group justify="space-between" wrap="wrap" gap="sm">
        <Text size="sm" c="dimmed">
          {t("ops:subtitles.fetchScope")}
        </Text>
        <Group gap="sm">
          <Badge variant="light" color={enabled ? "teal" : "gray"}>
            {enabled ? t("ops:subtitles.enabled") : t("ops:subtitles.disabled")}
          </Badge>
          <Button
            variant="default"
            leftSection={<IconListSearch size={16} />}
            loading={audit.isPending}
            onClick={() => audit.mutate({ body: { type: "subtitle_audit" } })}
          >
            {t("ops:subtitles.audit")}
          </Button>
          <Button
            leftSection={<IconDownload size={16} />}
            loading={running}
            disabled={!enabled}
            onClick={() => submit.mutate({ body: { type: "subtitle_fetch" } })}
          >
            {t("ops:subtitles.fetch")}
          </Button>
        </Group>
      </Group>
      <Group justify="space-between" wrap="wrap" gap="sm" align="flex-end">
        <Select
          label={t("ops:subtitles.libraryLabel")}
          description={t("ops:subtitles.libraryHint")}
          placeholder={t("ops:subtitles.libraryPlaceholder")}
          data={(librariesQuery.data?.items ?? []).map((library) => ({
            value: String(library.id),
            label: library.name,
          }))}
          value={libraryId === null ? null : String(libraryId)}
          onChange={(value) => setLibraryId(value === null ? null : Number(value))}
          clearable
          w={280}
        />
        <Group gap="sm">
          <Button
            variant="default"
            leftSection={<IconListSearch size={16} />}
            loading={audit.isPending}
            disabled={libraryId === null}
            onClick={() =>
              audit.mutate({ body: { type: "subtitle_audit", library_id: libraryId } })
            }
          >
            {t("ops:subtitles.auditLibrary")}
          </Button>
          <Button
            leftSection={<IconDownload size={16} />}
            loading={submit.isPending}
            disabled={!enabled || libraryId === null}
            onClick={() =>
              submit.mutate({
                body: { type: "subtitle_fetch", library_id: libraryId, limit: 200 },
              })
            }
          >
            {t("ops:subtitles.fetchLibrary")}
          </Button>
          <Button
            variant="default"
            leftSection={<IconMicrophone size={16} />}
            loading={generate.isPending}
            disabled={libraryId === null}
            onClick={() =>
              generate.mutate({ body: { type: "subtitle_generate", library_id: libraryId } })
            }
          >
            {t("ops:subtitles.generateLibrary")}
          </Button>
        </Group>
      </Group>
    </Stack>
  );
}
