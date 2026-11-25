import { Badge, Button, Card, Group, Select, Stack, Switch, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconFileText } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getNfoStatusOptions,
  getNfoStatusQueryKey,
  listTasksQueryKey,
} from "@/client/@tanstack/react-query.gen";
import { submitTask } from "@/client/sdk.gen";
import { extractErrorMessage } from "@/lib/api-error";

/**
 * NFO 修补: 给演员块补缩略图, 或删掉导演行.
 * 两类修补都改写磁盘上的 NFO, 因此默认只预演; 真跑需要显式关掉开关.
 */
export function NfoCard() {
  const { t } = useTranslation("ops");
  const queryClient = useQueryClient();
  const query = useQuery(getNfoStatusOptions());
  const [kind, setKind] = useState<string | null>("thumbs");
  const [libraryId, setLibraryId] = useState<string | null>(null);
  const [dryRun, setDryRun] = useState(true);

  const patchMutation = useMutation({
    mutationFn: async () => {
      await submitTask({
        body: {
          type: "nfo_patch",
          kind: kind === "directors" ? "directors" : "thumbs",
          library_id: libraryId === null ? null : Number(libraryId),
          dry_run: dryRun,
        },
        throwOnError: true,
      });
    },
    onSuccess: () => {
      notifications.show({ message: t("maintenance.nfo.queued"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: getNfoStatusQueryKey() });
      // 入队后让「最近结果」立即出现这一条, 不必等它下一轮轮询.
      void queryClient.invalidateQueries({ queryKey: listTasksQueryKey() });
    },
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("maintenance.nfo.runFailed")),
        color: "red",
      }),
  });

  const libraryOptions = (query.data?.libraries ?? []).map((item) => ({
    value: String(item.library_id),
    label: item.name,
  }));

  return (
    <Card withBorder padding="md">
      <Stack gap="sm">
        <Group justify="space-between" wrap="wrap">
          <Text fw={600}>{t("maintenance.nfo.title")}</Text>
          <Badge variant="light" color={(query.data?.actors_with_image ?? 0) > 0 ? "blue" : "gray"}>
            {t("maintenance.nfo.coverage", {
              withImage: query.data?.actors_with_image ?? 0,
              actors: query.data?.actors ?? 0,
            })}
          </Badge>
        </Group>
        <Text size="xs" c="dimmed">
          {t("maintenance.nfo.hint")}
        </Text>
        <Group align="flex-end" wrap="wrap" gap="sm">
          <Select
            label={t("maintenance.nfo.kind")}
            data={[
              { value: "thumbs", label: t("maintenance.nfo.kinds.thumbs") },
              { value: "directors", label: t("maintenance.nfo.kinds.directors") },
            ]}
            value={kind}
            onChange={setKind}
            w={200}
          />
          <Select
            label={t("maintenance.nfo.library")}
            placeholder={t("maintenance.nfo.allLibraries")}
            data={libraryOptions}
            value={libraryId}
            onChange={setLibraryId}
            clearable
            w={220}
          />
          <Switch
            label={t("maintenance.nfo.dryRun")}
            description={t("maintenance.nfo.dryRunHint")}
            checked={dryRun}
            onChange={(event) => setDryRun(event.currentTarget.checked)}
          />
          <Button
            leftSection={<IconFileText size={16} />}
            loading={patchMutation.isPending}
            onClick={() => patchMutation.mutate()}
          >
            {dryRun ? t("maintenance.nfo.preview") : t("maintenance.nfo.apply")}
          </Button>
        </Group>
      </Stack>
    </Card>
  );
}
