import { Badge, Button, Card, Group, Stack, Table, Text, Textarea, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { notifyStatusOptions, notifyStatusQueryKey } from "@/client/@tanstack/react-query.gen";
import { submitTask } from "@/client/sdk.gen";
import { extractErrorMessage } from "@/lib/api-error";

type Translate = TFunction<["ops"]>;

/** 渠道名固定四种: 用显式分支, 模板键会让抽取器插占位键. */
function channelLabel(t: Translate, kind: string): string {
  switch (kind) {
    case "bark":
      return t("notify.kinds.bark");
    case "serverchan":
      return t("notify.kinds.serverchan");
    case "telegram":
      return t("notify.kinds.telegram");
    default:
      return t("notify.kinds.webhook");
  }
}

function recordKindLabel(t: Translate, kind: string): string {
  return kind === "digest" ? t("notify.recordKinds.digest") : t("notify.recordKinds.message");
}

/**
 * 通知: 渠道是否就绪, 发一条测试消息, 看最近几次推送的结果.
 * 渠道类型与地址在设置页填写; 这里只报有没有填, 不回显地址.
 */
export function NotifyPanel() {
  const { t, i18n } = useTranslation("ops");
  const queryClient = useQueryClient();
  const query = useQuery({ ...notifyStatusOptions(), refetchOnMount: "always" as const });
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");

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
            value={title}
            onChange={(event) => setTitle(event.currentTarget.value)}
          />
          <Textarea
            label={t("notify.send.bodyLabel")}
            placeholder={t("notify.send.bodyPlaceholder")}
            autosize
            minRows={2}
            value={body}
            onChange={(event) => setBody(event.currentTarget.value)}
          />
          <Button
            onClick={() => sendMutation.mutate()}
            loading={sendMutation.isPending}
            disabled={!title.trim()}
          >
            {t("notify.send.run")}
          </Button>
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
          {records.length === 0 ? (
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
                        <Text size="xs">
                          {new Date(record.created_at).toLocaleString(i18n.language)}
                        </Text>
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
