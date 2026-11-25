import {
  Alert,
  Badge,
  Button,
  Group,
  Modal,
  Paper,
  PasswordInput,
  Select,
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
import type { EnvEntry, EnvKeyMeta } from "@/client/types.gen";

/** 提交给后端的一条; 敏感键原样带回占位符表示「不改」. */
type Draft = { key: string; value: string };

const PLACEHOLDER = "***";

/**
 * 启动期环境变量 (`AMANE_*`) 的编辑器.
 *
 * 与上半页的运行期配置分开: 这里的改动写进数据目录下的 `.env`, 重启进程才生效, 因此不做
 * 局部保存 —— 整份提交, 未列出的键表示删除 (回到默认值).
 *
 * 可编辑范围由后端给定 (`keys[].editable`): 数据目录、安全目录、token 这类改错就打不开
 * 页面的键不在其中; 被容器环境变量接管的键能写但不生效, 一律标出来并禁改.
 */
export function EnvEditor() {
  const { t } = useTranslation("settings");
  const queryClient = useQueryClient();
  const narrow = useNarrowViewport("md");
  const query = useQuery(getEnvOptions());
  const [picked, setPicked] = useState<string | null>(null);
  const [confirmRestart, setConfirmRestart] = useState(false);

  const entries = query.data?.entries;
  const supervised = query.data?.supervised === true;
  const metaOf = useMemo(() => {
    const map = new Map<string, EnvKeyMeta>();
    for (const item of query.data?.keys ?? []) map.set(item.key, item);
    return map;
  }, [query.data?.keys]);

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

  const available = (query.data?.keys ?? []).filter(
    (item) =>
      item.editable && !item.overridden_by_env && !drafts.some((draft) => draft.key === item.key),
  );

  const update = (key: string, value: string) =>
    setDrafts(drafts.map((draft) => (draft.key === key ? { key, value } : draft)));

  const remove = (key: string) => setDrafts(drafts.filter((draft) => draft.key !== key));

  const add = () => {
    if (picked === null) return;
    setDrafts([...drafts, { key: picked, value: "" }]);
    setPicked(null);
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
              const meta = metaOf.get(draft.key);
              const locked = meta?.overridden_by_env === true || meta?.editable === false;
              return (
                <Table.Tr key={draft.key}>
                  <Table.Td>
                    <Group gap={6} wrap="nowrap">
                      <Text size="sm" ff="monospace">
                        {draft.key}
                      </Text>
                      {meta?.secret === true && (
                        <Badge size="xs" color="yellow" variant="light">
                          {t("env.secret")}
                        </Badge>
                      )}
                      {meta?.overridden_by_env === true && (
                        <Tooltip label={t("env.overriddenHint")} multiline w={280} withArrow>
                          <Badge size="xs" color="gray" variant="light">
                            {t("env.overridden")}
                          </Badge>
                        </Tooltip>
                      )}
                      {meta?.description !== undefined && meta.description !== "" && (
                        <Tooltip label={meta.description} multiline w={260} withArrow>
                          <IconInfoCircle size={14} />
                        </Tooltip>
                      )}
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    {meta?.secret === true ? (
                      <PasswordInput
                        value={draft.value}
                        placeholder={PLACEHOLDER}
                        disabled={locked}
                        onChange={(event) => update(draft.key, event.currentTarget.value)}
                        styles={{ input: { fontFamily: "monospace" } }}
                      />
                    ) : (
                      <TextInput
                        value={draft.value}
                        disabled={locked}
                        onChange={(event) => update(draft.key, event.currentTarget.value)}
                        styles={{ input: { fontFamily: "monospace" } }}
                      />
                    )}
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
        <Select
          w={narrow ? "100%" : 320}
          searchable
          clearable
          value={picked}
          data={available.map((item) => ({
            value: item.key,
            label: item.description === "" ? item.key : `${item.key} · ${item.description}`,
          }))}
          placeholder={t("env.addPlaceholder")}
          nothingFoundMessage={t("env.addNothingFound")}
          onChange={setPicked}
        />
        <Button
          leftSection={<IconPlus size={16} />}
          variant="light"
          onClick={add}
          disabled={picked === null}
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
          onClick={() => setConfirmRestart(true)}
          loading={restart.isPending}
          disabled={dirty}
        >
          {t("env.restart")}
        </Button>
      </Group>

      <Text size="xs" c="dimmed">
        {supervised ? t("env.restartHint") : t("env.restartUnavailable")}
      </Text>

      <Modal
        opened={confirmRestart}
        onClose={() => setConfirmRestart(false)}
        title={t("env.restartConfirmTitle")}
        centered
      >
        <Stack gap="md">
          <Text size="sm">{t("env.restartConfirmBody")}</Text>
          <Group justify="flex-end" gap="xs">
            <Button variant="default" onClick={() => setConfirmRestart(false)}>
              {t("env.restartCancel")}
            </Button>
            <Button
              color="orange"
              disabled={!supervised}
              onClick={() => {
                setConfirmRestart(false);
                restart.mutate({});
              }}
            >
              {t("env.restartConfirm")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}

function toDraft(entry: EnvEntry): Draft {
  return { key: entry.key, value: entry.value };
}
