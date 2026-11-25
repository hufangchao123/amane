import {
  Badge,
  Button,
  Card,
  Group,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Text,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconDownload,
  IconListSearch,
  IconMicrophone,
  IconPlugConnected,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import {
  getConfigOptions,
  listLibrariesOptions,
  listTasksQueryKey,
  subtitleStatusOptions,
  subtitleStatusQueryKey,
  submitTaskMutation,
  testSubtitleGenerationMutation,
} from "@/client/@tanstack/react-query.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { isRecord } from "@/lib/utils";

/** 排队中与运行中的任务不会自己通知前端, 因此状态卡按固定间隔重取, 任务结束后自然回落. */
const STATUS_POLL_MS = 5000;

/** 「补全此库」单轮处理的份数; 后端上限 500, 默认值沿用原来的 200. */
const FETCH_LIMIT_DEFAULT = 200;
const FETCH_LIMIT_MAX = 500;

/** 转写开关只在整份配置里, 状态接口不下发: `/api/config` 的响应没有类型, 逐层收窄只取这一个布尔.
 *
 * 读不到就返回 null, 由调用方决定按哪种态度处理 —— 这里不猜 true.
 */
function readGenerationEnabled(config: unknown): boolean | null {
  if (!isRecord(config) || !isRecord(config.subtitles)) return null;
  const { generation } = config.subtitles;
  if (!isRecord(generation)) return null;
  return typeof generation.enabled === "boolean" ? generation.enabled : null;
}

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
  const [fetchLimit, setFetchLimit] = useState(FETCH_LIMIT_DEFAULT);
  const librariesQuery = useQuery(listLibrariesOptions());
  const configQuery = useQuery(getConfigOptions());
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

  /** 端点探测: 换机器或换端口后先点这里, 不用真跑一部片子才知道通不通. */
  const probe = useMutation({
    ...testSubtitleGenerationMutation(),
    onSuccess: (data) => {
      if (data.ok) {
        notifications.show({
          message: t("ops:subtitles.testOk", { model: data.model, ms: data.latency_ms }),
          color: "teal",
        });
        return;
      }
      notifications.show({ message: data.detail || t("ops:subtitles.testFailed"), color: "red" });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("ops:subtitles.testFailed")),
        color: "red",
      }),
  });

  const status = statusQuery.data;
  const enabled = status?.enabled ?? false;
  const running = submit.isPending;
  const generationEnabled = readGenerationEnabled(configQuery.data);
  // 份数越界时按边界提交: 输入框允许空值, 但任务载荷只能收 1..500.
  const fetchLimitSafe = Math.min(FETCH_LIMIT_MAX, Math.max(1, Math.round(fetchLimit) || 1));

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
          hint={[
            t("ops:subtitles.missingHint"),
            status?.multi_disc
              ? t("ops:subtitles.multiDiscHint", { parts: status.multi_disc })
              : "",
            status?.pointer ? t("ops:subtitles.pointerHint", { parts: status.pointer }) : "",
          ]
            .filter(Boolean)
            .join(" · ")}
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
          <NumberInput
            aria-label={t("ops:subtitles.fetchLimitLabel")}
            description={t("ops:subtitles.fetchLimitHint")}
            value={fetchLimit}
            onChange={(value) =>
              setFetchLimit(typeof value === "number" ? value : FETCH_LIMIT_DEFAULT)
            }
            min={1}
            max={FETCH_LIMIT_MAX}
            allowDecimal={false}
            w={120}
          />
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
                body: { type: "subtitle_fetch", library_id: libraryId, limit: fetchLimitSafe },
              })
            }
          >
            {t("ops:subtitles.fetchLibrary")}
          </Button>
          <Button
            variant="subtle"
            leftSection={<IconPlugConnected size={16} />}
            loading={probe.isPending}
            onClick={() => probe.mutate({})}
          >
            {t("ops:subtitles.testGeneration")}
          </Button>
          <Button
            variant="default"
            leftSection={<IconMicrophone size={16} />}
            loading={generate.isPending}
            disabled={libraryId === null || generationEnabled !== true}
            onClick={() =>
              generate.mutate({ body: { type: "subtitle_generate", library_id: libraryId } })
            }
          >
            {t("ops:subtitles.generateLibrary")}
          </Button>
          {generationEnabled === false ? (
            <Text size="xs" c="dimmed">
              {t("ops:subtitles.generationDisabled")}
            </Text>
          ) : null}
        </Group>
      </Group>
    </Stack>
  );
}
