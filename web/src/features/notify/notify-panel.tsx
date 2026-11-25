import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Stack,
  Table,
  Text,
  Textarea,
  TextInput,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconAlertTriangle, IconRefresh, IconReportAnalytics } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { notifyStatusOptions, notifyStatusQueryKey } from "@/client/@tanstack/react-query.gen";
import { submitTask } from "@/client/sdk.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { formatDateTime } from "@/lib/utils";

type Translate = TFunction<["ops"]>;

/** 与后端 `NOTIFY_TITLE_LIMIT` / `NOTIFY_BODY_LIMIT` 一致; 超长会被 422 拒掉, 因此在输入处就截住. */
const TITLE_LIMIT = 200;
const BODY_LIMIT = 2000;

/**
 * 渠道名固定四种: 用显式分支, 模板键会让抽取器插占位键.
 *
 * 键带 `ops:` 前缀: 这里的 `t` 是参数, 抽取器读不出它的命名空间, 会把这些键记到默认命名空间.
 */
function channelLabel(t: Translate, kind: string): string {
  switch (kind) {
    case "bark":
      return t("ops:notify.kinds.bark");
    case "serverchan":
      return t("ops:notify.kinds.serverchan");
    case "telegram":
      return t("ops:notify.kinds.telegram");
    default:
      return t("ops:notify.kinds.webhook");
  }
}

function recordKindLabel(t: Translate, kind: string): string {
  return kind === "digest"
    ? t("ops:notify.recordKinds.digest")
    : t("ops:notify.recordKinds.message");
}

/**
 * 通知: 渠道是否就绪, 发一条测试消息, 看最近几次推送的结果.
 * 渠道类型与地址在设置页填写; 这里只报有没有填, 不回显地址.
 */
export function NotifyPanel() {
  const { t, i18n } = useTranslation("ops");
  const { t: tCommon } = useTranslation("common");
  const queryClient = useQueryClient();
  const query = useQuery({ ...notifyStatusOptions(), refetchOnMount: "always" as const });
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");

  /** 渠道没启用或没填地址时, 入队也只会失败; 按钮直接禁用, 省掉一次注定失败的任务. */
  const channelReady = (query.data?.enabled ?? false) && (query.data?.has_url ?? false);

  const sendMutation = useMutation({
    mutationFn: async () => {
      await submitTask({
        body: { type: "notify", kind: "message", title, body },
        throwOnError: true,
      });
    },
    onSuccess: () => {
      notifications.show({ message: t("notify.queued"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: notifyStatusQueryKey() });
    },
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("notify.runFailed")),
        color: "red",
      }),
  });

  const digestMutation = useMutation({
    mutationFn: async () => {
      // 周报的标题与正文由后端现算, 这里不传.
      await submitTask({ body: { type: "notify", kind: "digest" }, throwOnError: true });
    },
    onSuccess: () => {
      notifications.show({ message: t("notify.digestQueued"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: notifyStatusQueryKey() });
    },
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("notify.runFailed")),
        color: "red",
      }),
  });

  const records = query.data?.records ?? [];

  return (
    <Stack gap="md">
      <Card withBorder padding="md">
        <Stack gap="sm">
          <Group justify="space-between" wrap="wrap">
            <Text fw={600}>{t("notify.channel.title")}</Text>
            <Badge
              variant="light"
              color={query.data?.enabled && query.data.has_url ? "blue" : "gray"}
            >
              {query.data?.enabled
                ? t("notify.channel.enabled", { kind: channelLabel(t, query.data.kind) })
                : t("notify.channel.disabled")}
            </Badge>
          </Group>
          <Text size="xs" c="dimmed">
            {query.data?.has_url ? t("notify.channel.urlSet") : t("notify.channel.urlMissing")}
          </Text>
          <Text size="xs" c="dimmed">
            {t("notify.channel.hint")}
          </Text>
        </Stack>
      </Card>

      <Card withBorder padding="md">
        <Stack gap="sm">
          <Text fw={600}>{t("notify.send.title")}</Text>
          <TextInput
            label={t("notify.send.titleLabel")}
            placeholder={t("notify.send.titlePlaceholder")}
            description={t("notify.send.titleCounter", {
              used: title.length,
              limit: TITLE_LIMIT,
            })}
            maxLength={TITLE_LIMIT}
            value={title}
            onChange={(event) => setTitle(event.currentTarget.value)}
          />
          <Textarea
            label={t("notify.send.bodyLabel")}
            placeholder={t("notify.send.bodyPlaceholder")}
            description={t("notify.send.bodyCounter", {
              used: body.length,
              limit: BODY_LIMIT,
            })}
            autosize
            minRows={2}
            maxLength={BODY_LIMIT}
            value={body}
            onChange={(event) => setBody(event.currentTarget.value)}
          />
          <Group gap="sm">
            <Button
              onClick={() => sendMutation.mutate()}
              loading={sendMutation.isPending}
              disabled={!title.trim() || !channelReady}
            >
              {t("notify.send.run")}
            </Button>
            <Button
              variant="light"
              leftSection={<IconReportAnalytics size={16} />}
              onClick={() => digestMutation.mutate()}
              loading={digestMutation.isPending}
              disabled={!channelReady}
            >
              {t("notify.send.digest")}
            </Button>
            <Text size="xs" c="dimmed">
              {t("notify.send.digestHint")}
            </Text>
          </Group>
        </Stack>
      </Card>

      <Card withBorder padding="md">
        <Stack gap="sm">
          <Group justify="space-between" wrap="wrap">
            <Text fw={600}>{t("notify.records.title")}</Text>
            <Button
              variant="light"
              size="xs"
              onClick={() => void query.refetch()}
              loading={query.isFetching}
            >
              {t("notify.records.refresh")}
            </Button>
          </Group>
          {query.isError ? (
            <Alert
              color="red"
              variant="light"
              icon={<IconAlertTriangle size={16} />}
              title={t("notify.records.loadFailed")}
            >
              <Stack gap="xs" align="flex-start">
                <Text size="xs">
                  {extractErrorMessage(query.error, tCommon("toast.operationFailed"))}
                </Text>
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<IconRefresh size={14} />}
                  loading={query.isFetching}
                  onClick={() => void query.refetch()}
                >
                  {tCommon("actions.retry")}
                </Button>
              </Stack>
            </Alert>
          ) : records.length === 0 ? (
            <Text size="sm" c="dimmed">
              {t("notify.records.empty")}
            </Text>
          ) : (
            <Table.ScrollContainer minWidth={640}>
              <Table striped withTableBorder>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>{t("notify.records.columns.time")}</Table.Th>
                    <Table.Th>{t("notify.records.columns.kind")}</Table.Th>
                    <Table.Th>{t("notify.records.columns.channel")}</Table.Th>
                    <Table.Th>{t("notify.records.columns.title")}</Table.Th>
                    <Table.Th>{t("notify.records.columns.result")}</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {records.map((record, index) => (
                    <Table.Tr key={`${record.created_at}-${index}`}>
                      <Table.Td>
                        <Text size="xs">{formatDateTime(record.created_at, i18n.language)}</Text>
                      </Table.Td>
                      <Table.Td>
                        <Text size="xs">{recordKindLabel(t, record.kind)}</Text>
                      </Table.Td>
                      <Table.Td>
                        <Text size="xs">{record.channel}</Text>
                      </Table.Td>
                      <Table.Td>
                        <Text size="xs" lineClamp={1}>
                          {record.title}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Badge variant="light" color={record.ok ? "teal" : "red"}>
                          {record.ok
                            ? t("notify.records.ok")
                            : record.error || t("notify.records.failed")}
                        </Badge>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )}
        </Stack>
      </Card>
    </Stack>
  );
}
