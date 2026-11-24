import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Loader,
  Select,
  Stack,
  Table,
  Text,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconAlertCircle, IconReload, IconTrashX } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  clearAuditMutation,
  listAuditOptions,
  listAuditQueryKey,
} from "@/client/@tanstack/react-query.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { formatDateTime } from "@/lib/utils";

const METHOD_FILTERS = ["", "POST", "PUT", "PATCH", "DELETE"] as const;

/**
 * 写操作留痕: 谁在什么时候改了哪条路径.
 * 记录由请求路径上的队列异步落库, 因此这里的列表可能比刚发生的操作晚一拍.
 */
export function AuditPanel() {
  const { t, i18n } = useTranslation("ops");
  const { t: tCommon } = useTranslation("common");
  const queryClient = useQueryClient();
  const [method, setMethod] = useState<string>("");
  const query = useQuery(listAuditOptions({ query: { limit: 100, method: method || undefined } }));

  const clearMutation = useMutation({
    ...clearAuditMutation(),
    onSuccess: async () => {
      notifications.show({ message: t("audit.cleared"), color: "teal" });
      await queryClient.invalidateQueries({ queryKey: listAuditQueryKey() });
    },
    onError: (error) => {
      notifications.show({
        message: extractErrorMessage(error, t("audit.clearFailed")),
        color: "red",
      });
    },
  });

  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;

  async function startClear() {
    const ok = await confirm({
      title: t("audit.confirmClearTitle"),
      message: t("audit.confirmClearMessage"),
    });
    if (!ok) return;
    clearMutation.mutate({});
  }

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end">
        <Group gap="sm" align="flex-end">
          <Select
            label={t("audit.filter.method")}
            data={METHOD_FILTERS.map((value) => ({ value, label: value || t("audit.filter.all") }))}
            value={method}
            onChange={(value) => setMethod(value ?? "")}
            w={180}
          />
          <Button
            variant="light"
            leftSection={<IconReload size={16} />}
            onClick={() => void query.refetch()}
            loading={query.isFetching}
          >
            {t("audit.refresh")}
          </Button>
        </Group>
        <Button
          color="red"
          variant="light"
          leftSection={<IconTrashX size={16} />}
          loading={clearMutation.isPending}
          onClick={() => void startClear()}
        >
          {t("audit.clear")}
        </Button>
      </Group>

      <Card withBorder padding="md">
        {query.isError ? (
          <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />}>
            <Text size="sm">{t("audit.loadFailed")}</Text>
            <Text size="xs" c="dimmed">
              {extractErrorMessage(query.error, tCommon("toast.operationFailed"))}
            </Text>
          </Alert>
        ) : (
          <>
            <Text size="sm" c="dimmed">
              {t("audit.summary", { total })}
            </Text>
            {query.isLoading ? (
              <Group justify="center" py="md">
                <Loader size="sm" />
              </Group>
            ) : items.length === 0 ? (
              <Text size="sm" c="dimmed" py="md">
                {t("audit.empty")}
              </Text>
            ) : (
              <Table.ScrollContainer minWidth={720}>
                <Table striped highlightOnHover>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>{t("audit.columns.time")}</Table.Th>
                      <Table.Th>{t("audit.columns.method")}</Table.Th>
                      <Table.Th>{t("audit.columns.path")}</Table.Th>
                      <Table.Th>{t("audit.columns.status")}</Table.Th>
                      <Table.Th>{t("audit.columns.client")}</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {items.map((item) => (
                      <Table.Tr key={item.id}>
                        <Table.Td>{formatDateTime(item.created_at, i18n.language)}</Table.Td>
                        <Table.Td>
                          <Badge variant="light">{item.method}</Badge>
                        </Table.Td>
                        <Table.Td>
                          <Text size="sm" ff="monospace">
                            {item.path}
                            {item.query ? `?${item.query}` : ""}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          <Badge color={item.status < 400 ? "teal" : "red"} variant="light">
                            {item.status}
                          </Badge>
                        </Table.Td>
                        <Table.Td>
                          <Text size="sm" c="dimmed">
                            {item.client || "-"}
                          </Text>
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            )}
          </>
        )}
      </Card>
    </Stack>
  );
}
