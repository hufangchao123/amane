import { Button, Card, Group, NumberInput, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconArrowsMaximize } from "@tabler/icons-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { listTasksQueryKey } from "@/client/@tanstack/react-query.gen";
import { submitTask } from "@/client/sdk.gen";
import { extractErrorMessage } from "@/lib/api-error";

/** 单次最多处理多少份; 阈值本身留在配置里 (`sr.max_dim_threshold` / `sr.max_bytes_threshold`). */
const LIMIT_DEFAULT = 200;
const LIMIT_MAX = 1000;

/** 超分: 按阈值把偏小的图放大. 任务只入队, 结果在下面的「最近结果」里看. */
export function UpscaleCard() {
  const { t } = useTranslation("ops");
  const queryClient = useQueryClient();
  const [limit, setLimit] = useState<number | string>(LIMIT_DEFAULT);
  const limitSafe = Math.min(LIMIT_MAX, Math.max(1, Math.round(Number(limit) || LIMIT_DEFAULT)));

  const runMutation = useMutation({
    mutationFn: async () => {
      await submitTask({ body: { type: "upscale", limit: limitSafe }, throwOnError: true });
    },
    onSuccess: () => {
      notifications.show({ message: t("maintenance.upscale.queued"), color: "blue" });
      // 入队后让「最近结果」立即出现这一条, 不必等它下一轮轮询.
      void queryClient.invalidateQueries({ queryKey: listTasksQueryKey() });
    },
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("maintenance.upscale.runFailed")),
        color: "red",
      }),
  });

  return (
    <Card withBorder padding="md">
      <Stack gap="sm">
        <Text fw={600}>{t("maintenance.upscale.title")}</Text>
        <Text size="sm" c="dimmed">
          {t("maintenance.upscale.hint")}
        </Text>
        <Group align="flex-end" gap="sm">
          <NumberInput
            label={t("maintenance.upscale.limit")}
            value={limit}
            onChange={setLimit}
            min={1}
            max={LIMIT_MAX}
            allowDecimal={false}
            w={160}
          />
          <Button
            leftSection={<IconArrowsMaximize size={16} />}
            loading={runMutation.isPending}
            onClick={() => runMutation.mutate()}
          >
            {t("maintenance.upscale.run")}
          </Button>
        </Group>
        <Text size="xs" c="dimmed">
          {t("maintenance.upscale.note")}
        </Text>
      </Stack>
    </Card>
  );
}
