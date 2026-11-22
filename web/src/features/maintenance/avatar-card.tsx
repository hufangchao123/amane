import { Button, Card, Group, NumberInput, Stack, Switch, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconUserCheck } from "@tabler/icons-react";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { submitTask } from "@/client/sdk.gen";
import { extractErrorMessage } from "@/lib/api-error";

/**
 * 头像校验: 抽查若干演员的首图, 可选把失效链接摘掉.
 * 校验只读; 摘链接才写库, 因此默认不开.
 */
export function AvatarCard() {
  const { t } = useTranslation("ops");
  const [limit, setLimit] = useState<number | string>(20);
  const [clearBroken, setClearBroken] = useState(false);

  const checkMutation = useMutation({
    mutationFn: async () => {
      await submitTask({
        body: { type: "avatar_check", limit: Number(limit) || 20, clear_broken: clearBroken },
        throwOnError: true,
      });
    },
    onSuccess: () => notifications.show({ message: t("maintenance.avatar.queued"), color: "blue" }),
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("maintenance.avatar.runFailed")),
        color: "red",
      }),
  });

  return (
    <Card withBorder padding="md">
      <Stack gap="sm">
        <Text fw={600}>{t("maintenance.avatar.title")}</Text>
        <Text size="sm" c="dimmed">
          {t("maintenance.avatar.hint")}
        </Text>
        <Group align="flex-end" gap="sm">
          <NumberInput
            label={t("maintenance.avatar.limit")}
            value={limit}
            onChange={setLimit}
            min={1}
            max={200}
            w={160}
          />
          <Switch
            label={t("maintenance.avatar.clearBroken")}
            checked={clearBroken}
            onChange={(event) => setClearBroken(event.currentTarget.checked)}
          />
          <Button
            leftSection={<IconUserCheck size={16} />}
            loading={checkMutation.isPending}
            onClick={() => checkMutation.mutate()}
          >
            {t("maintenance.avatar.run")}
          </Button>
        </Group>
        <Text size="xs" c="dimmed">
          {t("maintenance.avatar.note")}
        </Text>
      </Stack>
    </Card>
  );
}
