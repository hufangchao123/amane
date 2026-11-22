import { Badge, Group, Paper, Stack, Table, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { listTransferItemsOptions } from "@/client/@tanstack/react-query.gen";
import { ListPagination } from "@/components/common/list-pagination";
import { PageSizeSelect } from "@/components/common/page-size-select";
import { useUIStore } from "@/stores/ui";

import { STATE_COLORS } from "./state";

/** 最近记录: 只读列表, 逐文件状态就是任务最后写下的那一行. */
export function RecentItems() {
  const { t } = useTranslation("ops");
  const [page, setPage] = useState(1);
  const pageSize = useUIStore((s) => s.pageSizes.transferItems);
  const query = useQuery(
    listTransferItemsOptions({ query: { offset: (page - 1) * pageSize, limit: pageSize } }),
  );

  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Title order={4}>{t("transfer.recent.title")}</Title>
        <Group gap="sm">
          <Text size="sm" c="dimmed">
            {t("transfer.recent.total", { total })}
          </Text>
          <PageSizeSelect sizeKey="transferItems" onChanged={() => setPage(1)} />
        </Group>
      </Group>
      <Paper withBorder radius="md">
        <Table.ScrollContainer minWidth={640}>
          <Table highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("transfer.columns.name")}</Table.Th>
                <Table.Th w={96}>{t("transfer.columns.state")}</Table.Th>
                <Table.Th w={72}>{t("transfer.columns.attempts")}</Table.Th>
                <Table.Th>{t("transfer.columns.detail")}</Table.Th>
                <Table.Th w={176}>{t("transfer.columns.updated")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((item) => (
                <Table.Tr key={item.id}>
                  <Table.Td>
                    <Text size="sm" lineClamp={1}>
                      {item.file_name}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge color={STATE_COLORS[item.state]} variant="light">
                      {t(`transfer.state.${item.state}`)}
                    </Badge>
                  </Table.Td>
                  <Table.Td>{item.attempts}</Table.Td>
                  <Table.Td>
                    <Text size="sm" c="dimmed" lineClamp={1}>
                      {item.error ?? "-"}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" c="dimmed">
                      {new Date(item.updated_at).toLocaleString()}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        {items.length === 0 && (
          <Text c="dimmed" size="sm" ta="center" py="xl">
            {t("transfer.recent.empty")}
          </Text>
        )}
      </Paper>
      {totalPages > 1 && (
        <Group justify="center">
          <ListPagination totalPages={totalPages} page={page} onChange={setPage} />
        </Group>
      )}
    </Stack>
  );
}
