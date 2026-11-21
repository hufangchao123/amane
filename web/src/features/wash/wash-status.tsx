import { Alert, Button, Group, Paper, SimpleGrid, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconDeviceFloppy, IconRefresh, IconScan } from "@tabler/icons-react";
import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { submitTaskMutation } from "@/client/@tanstack/react-query.gen";
import type { WashStatusResponse } from "@/client/types.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { formatFileSize } from "@/lib/utils";
import { useWashRefresh } from "./use-wash-refresh";

interface WashStatusProps {
  status: WashStatusResponse;
  onRefresh: () => void;
}

/** 状态卡与两个直接入队的动作.
 *
 * 扫描与执行都只是入队: 页面拿不到任务进度, 所以按钮只负责提示「已提交」, 真正的数字由刷新重取.
 * 执行是移动库内文件的动作, 先确认再入队; 扫描只读, 不打扰.
 */
export function WashStatus({ status, onRefresh }: WashStatusProps) {
  const { t } = useTranslation(["ops", "common"]);
  const refresh = useWashRefresh();
  const scan = useMutation({
    ...submitTaskMutation(),
    onSuccess: () => {
      notifications.show({ message: t("wash.scanStarted"), color: "blue" });
      refresh();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });
  const apply = useMutation({
    ...submitTaskMutation(),
    onSuccess: () => {
      notifications.show({ message: t("wash.applyStarted"), color: "blue" });
      refresh();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  async function startApply() {
    const ok = await confirm({
      title: t("wash.confirmApplyTitle"),
      message: t("wash.confirmApplyMessage"),
    });
    if (!ok) return;
    // 空 group_keys 表示「重算后执行全部候选」: 页面不缓存上一轮扫描结果, 免得洗到已不存在的组.
    apply.mutate({ body: { type: "wash_apply" } });
  }

  return (
    <Stack gap="sm">
      {status.enabled ? null : <Alert color="yellow">{t("wash.disabled")}</Alert>}
      <SimpleGrid cols={{ base: 1, sm: 3 }}>
        <Stat label={t("wash.statGroups")} value={status.groups} />
        <Stat label={t("wash.statRestorable")} value={formatFileSize(status.restorable_bytes)} />
        <Stat label={t("wash.statTrashed")} value={status.trashed} />
      </SimpleGrid>
      <Group>
        <Button
          leftSection={<IconScan size={16} />}
          loading={scan.isPending}
          disabled={!status.enabled}
          onClick={() => scan.mutate({ body: { type: "wash_scan" } })}
        >
          {t("wash.scan")}
        </Button>
        <Button
          color="orange"
          leftSection={<IconDeviceFloppy size={16} />}
          loading={apply.isPending}
          disabled={!status.enabled || status.groups === 0}
          onClick={() => void startApply()}
        >
          {t("wash.apply")}
        </Button>
        <Button variant="default" leftSection={<IconRefresh size={16} />} onClick={onRefresh}>
          {t("wash.refresh")}
        </Button>
      </Group>
    </Stack>
  );
}

interface StatProps {
  label: string;
  value: number | string;
}

function Stat({ label, value }: StatProps) {
  return (
    <Paper withBorder p="md">
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text fw={600} size="lg">
        {value}
      </Text>
    </Paper>
  );
}
