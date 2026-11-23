import { Alert, Badge, Center, Group, Loader, Stack, Table, Text, Title } from "@mantine/core";
import { IconAlertCircle } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { washCandidatesOptions } from "@/client/@tanstack/react-query.gen";
import type { WashRecordKind } from "@/client/types.gen";
import { ListPagination } from "@/components/common/list-pagination";
import { PageSizeSelect } from "@/components/common/page-size-select";
import { extractErrorMessage } from "@/lib/api-error";
import { formatFileSize } from "@/lib/utils";
import { useUIStore } from "@/stores/ui";
import { groupNumber, pathName } from "./path-name";

const KIND_COLOR: Record<WashRecordKind, string> = { local: "blue", strm: "grape" };
const KIND_LABEL: Record<WashRecordKind, "wash.kind.local" | "wash.kind.strm"> = {
  local: "wash.kind.local",
  strm: "wash.kind.strm",
};

/** 候选分组表: 只读, 数据来自索引与磁盘现状的合成, 不在前端做任何挑选. */
export function WashCandidates() {
  const { t } = useTranslation(["ops", "common"]);
  const [page, setPage] = useState(1);
  const pageSize = useUIStore((s) => s.pageSizes.washCandidates);
  const query = useQuery(
    washCandidatesOptions({
      query: {
        limit: pageSize,
        offset: (page - 1) * pageSize,
        sort_by: "bytes_freed",
        order: "desc",
      },
    }),
  );
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <Title order={4}>{t("wash.candidateTitle")}</Title>
        <PageSizeSelect sizeKey="washCandidates" onChanged={() => setPage(1)} />
      </Group>
      {query.isError ? (
        <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />}>
          <Text size="sm">{t("wash.candidatesLoadFailed")}</Text>
          <Text size="xs" c="dimmed">
            {extractErrorMessage(query.error, t("common:toast.operationFailed"))}
          </Text>
        </Alert>
      ) : query.isPending ? (
        <Center py="md">
          <Loader size="sm" />
        </Center>
      ) : items.length === 0 ? (
        <Text c="dimmed" size="sm">
          {t("wash.candidateEmpty")}
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={720}>
          <Table highlightOnHover verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("wash.column.group")}</Table.Th>
                <Table.Th>{t("wash.column.kind")}</Table.Th>
                <Table.Th>{t("wash.column.keep")}</Table.Th>
                <Table.Th>{t("wash.column.drops")}</Table.Th>
                <Table.Th>{t("wash.column.bytes")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((item) => {
                const drops = item.drop_paths ?? [];
                return (
                  <Table.Tr key={`${item.kind}:${item.group_key}`}>
                    <Table.Td>
                      <Text size="sm">{groupNumber(item.group_key)}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Badge color={KIND_COLOR[item.kind]} variant="light">
                        {t(KIND_LABEL[item.kind])}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{pathName(item.keep_path)}</Text>
                      <Text size="xs" c="dimmed">
                        {formatFileSize(item.keep_size ?? 0)}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{drops.length}</Text>
                      <Text size="xs" c="dimmed">
                        {drops.map(pathName).join(", ")}
                      </Text>
                    </Table.Td>
                    <Table.Td>{formatFileSize(item.bytes_freed ?? 0)}</Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
      <ListPagination totalPages={totalPages} page={page} onChange={setPage} />
    </Stack>
  );
}
