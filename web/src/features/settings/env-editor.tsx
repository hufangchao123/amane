import {
  Alert,
  Badge,
  Button,
  Group,
  Paper,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconAlertCircle,
  IconDeviceFloppy,
  IconInfoCircle,
  IconPlus,
  IconReload,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNarrowViewport } from "@/hooks/use-narrow-viewport";
import { extractErrorMessage } from "@/lib/api-error";
import {
  getEnvOptions,
  getEnvQueryKey,
  putEnvMutation,
  restartServerMutation,
} from "@/client/@tanstack/react-query.gen";
import type { EnvEntry } from "@/client/types.gen";

/** 提交给后端的一条; 敏感键原样带回占位符表示「不改」. */
type Draft = { key: string; value: string };

const PLACEHOLDER = "***";

/**
 * 启动期环境变量 (`AMANE_*`) 的编辑器.
 *
 * 与上半页的运行期配置分开: 这里的改动写进数据目录下的 `.env`, 重启进程才生效, 因此不做
 * 局部保存 —— 整份提交, 未列出的键表示删除 (回到默认值).
 */
export function EnvEditor() {
  const { t } = useTranslation("settings");
  const queryClient = useQueryClient();
  const narrow = useNarrowViewport("md");
  const query = useQuery(getEnvOptions());
  const [newKey, setNewKey] = useState("");

  const entries = query.data?.entries;
  // 以「服务端这一版的指纹」为准: 数据换了 (保存成功 / 重新拉取) 就丢弃本地草稿,
  // 不用 effect 同步 state —— 那会在渲染后多跑一轮, 也容易把用户正在输入的内容冲掉。
  const fingerprint = useMemo(() => JSON.stringify((entries ?? []).map(toDraft)), [entries]);
  const [edited, setEdited] = useState<{ base: string; drafts: Draft[] } | null>(null);
  const drafts =
    edited !== null && edited.base === fingerprint ? edited.drafts : (entries ?? []).map(toDraft);
  const dirty = JSON.stringify(drafts) !== fingerprint;

  const setDrafts = (next: Draft[]) => setEdited({ base: fingerprint, drafts: next });

  const save = useMutation({
    ...putEnvMutation(),
    onSuccess: (data) => {
      queryClient.setQueryData(getEnvQueryKey(), data);
      setEdited(null);
      notifications.show({ message: t("env.saved"), color: "teal" });
    },
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("env.saveFailed")),
        color: "red",
      }),
  });

  const restart = useMutation({
    ...restartServerMutation(),
    onSuccess: () => notifications.show({ message: t("env.restarting"), color: "blue" }),
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("env.restartFailed")),
        color: "red",
      }),
  });

  const available = (query.data?.editable_keys ?? []).filter(
    (key) => !drafts.some((draft) => draft.key === key),
  );

  const update = (key: string, value: string) =>
    setDrafts(drafts.map((draft) => (draft.key === key ? { key, value } : draft)));

  const remove = (key: string) => setDrafts(drafts.filter((draft) => draft.key !== key));

  const add = () => {
    if (!newKey) return;
    setDrafts([...drafts, { key: newKey, value: "" }]);
    setNewKey("");
  };

  return (
    <Stack gap="md">
      <Paper withBorder p="md">
        <Stack gap="xs">
          <Text fw={600}>{t("env.title")}</Text>
          <Text size="sm" c="dimmed">
            {t("env.hint")}
          </Text>
          <Text size="xs" c="dimmed">
            {query.data ? t("env.path", { path: query.data.path }) : t("env.loading")}
          </Text>
        </Stack>
      </Paper>

      {query.error && (
        <Alert icon={<IconAlertCircle size={18} />} color="red" variant="light">
          {extractErrorMessage(query.error, t("env.loadFailed"))}
        </Alert>
      )}

      <Table.ScrollContainer minWidth={narrow ? 0 : 640}>
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("env.columns.key")}</Table.Th>
              <Table.Th>{t("env.columns.value")}</Table.Th>
              <Table.Th w={80}>{t("env.columns.actions")}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {drafts.map((draft) => {
              const secret =
                (entries ?? []).find((entry) => entry.key === draft.key)?.secret === true;
              const description =
                (entries ?? []).find((entry) => entry.key === draft.key)?.description ?? "";
              return (
                <Table.Tr key={draft.key}>
                  <Table.Td>
                    <Group gap={6} wrap="nowrap">
                      <Text size="sm" ff="monospace">
                        {draft.key}
                      </Text>
                      {secret && (
                        <Badge size="xs" color="yellow" variant="light">
                          {t("env.secret")}
                        </Badge>
                      )}
                      {description && (
                        <Tooltip label={description} multiline w={260} withArrow>
                          <IconInfoCircle size={14} />
                        </Tooltip>
                      )}
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    <TextInput
                      value={draft.value}
                      placeholder={secret ? PLACEHOLDER : ""}
                      onChange={(event) => update(draft.key, event.currentTarget.value)}
                      styles={{ input: { fontFamily: "monospace" } }}
                    />
                  </Table.Td>
                  <Table.Td>
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      color="red"
                      onClick={() => remove(draft.key)}
                    >
                      {t("env.remove")}
                    </Button>
                  </Table.Td>
                </Table.Tr>
              );
            })}
            {drafts.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={3}>
                  <Text size="sm" c="dimmed">
                    {t("env.empty")}
                  </Text>
                </Table.Td>
              </Table.Tr>
            )}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>

      <Group gap="xs" wrap={narrow ? "wrap" : "nowrap"}>
        <TextInput
          w={narrow ? "100%" : 280}
          value={newKey}
          placeholder={t("env.addPlaceholder")}
          onChange={(event) => setNewKey(event.currentTarget.value.trim().toUpperCase())}
        />
        <Button
          leftSection={<IconPlus size={16} />}
          variant="light"
          onClick={add}
          disabled={!available.includes(newKey)}
        >
          {t("env.add")}
        </Button>
        <Button
          leftSection={<IconDeviceFloppy size={16} />}
          onClick={() => save.mutate({ body: { entries: drafts.filter((draft) => draft.key) } })}
          loading={save.isPending}
          disabled={!dirty}
        >
          {t("env.save")}
        </Button>
        <Button
          leftSection={<IconReload size={16} />}
          variant="light"
          color="orange"
          onClick={() => restart.mutate({})}
          loading={restart.isPending}
          disabled={dirty}
        >
          {t("env.restart")}
        </Button>
      </Group>

      <Text size="xs" c="dimmed">
        {t("env.restartHint")}
      </Text>
    </Stack>
  );
}

function toDraft(entry: EnvEntry): Draft {
  return { key: entry.key, value: entry.value };
}
