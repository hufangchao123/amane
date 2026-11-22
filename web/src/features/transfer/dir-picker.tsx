import {
  Alert,
  Badge,
  Breadcrumbs,
  Button,
  Card,
  Group,
  Stack,
  Table,
  Text,
  UnstyledButton,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { transferDirsOptions } from "@/client/@tanstack/react-query.gen";
import { useConfig, useUpdateConfig } from "@/hooks/use-config";

interface TrailEntry {
  id: string;
  name: string;
}

const ROOT_ID = "0";

/**
 * 网盘目录浏览: 逐层点进文件夹, 把当前这一层的目录 ID 填进源目录或目标目录.
 * 115 的源目录与目标目录只认 ID, 手填容易填错, 所以这里给一条点选路径.
 */
export function DirPicker() {
  const { t } = useTranslation("ops");
  const { data: config } = useConfig();
  const save = useUpdateConfig();
  const [trail, setTrail] = useState<TrailEntry[]>([
    { id: ROOT_ID, name: t("transfer.dirs.root") },
  ]);
  const current = trail[trail.length - 1];

  const dirs = useQuery({ ...transferDirsOptions({ query: { cid: current.id } }), retry: false });
  const sourceCid = config?.transfer?.source_cid ?? "";
  const targetCid = config?.transfer?.target_cid ?? "";

  const setDir = (field: "source_cid" | "target_cid") => {
    save.mutate({ body: { transfer: { [field]: current.id } } });
  };

  return (
    <Card withBorder>
      <Stack gap="sm">
        <Group justify="space-between">
          <Text fw={600}>{t("transfer.dirs.title")}</Text>
          <Button
            size="xs"
            variant="subtle"
            onClick={() => setTrail([{ id: ROOT_ID, name: t("transfer.dirs.root") }])}
          >
            {t("transfer.dirs.backToRoot")}
          </Button>
        </Group>
        <Text size="xs" c="dimmed">
          {t("transfer.dirs.hint")}
        </Text>
        <Group gap="xs">
          <Text size="sm">{t("transfer.dirs.current")}</Text>
          <Breadcrumbs>
            {trail.map((entry, index) => (
              <UnstyledButton key={entry.id} onClick={() => setTrail(trail.slice(0, index + 1))}>
                <Text size="sm" c={index === trail.length - 1 ? undefined : "blue"}>
                  {entry.name}
                </Text>
              </UnstyledButton>
            ))}
          </Breadcrumbs>
          <Badge variant="light">{t("transfer.dirs.id", { id: current.id })}</Badge>
        </Group>
        <Group gap="xs">
          <Button size="xs" loading={save.isPending} onClick={() => setDir("source_cid")}>
            {t("transfer.dirs.asSource")}
          </Button>
          <Button size="xs" loading={save.isPending} onClick={() => setDir("target_cid")}>
            {t("transfer.dirs.asTarget")}
          </Button>
          <Badge color={sourceCid === current.id ? "teal" : "gray"} variant="light">
            {t("transfer.dirs.sourceNow", { id: sourceCid || t("transfer.dirs.unset") })}
          </Badge>
          <Badge color={targetCid === current.id ? "teal" : "gray"} variant="light">
            {t("transfer.dirs.targetNow", { id: targetCid || t("transfer.dirs.unset") })}
          </Badge>
        </Group>
        {dirs.isError ? (
          <Alert color="red" variant="light">
            {t("transfer.dirs.loadFailed")}
          </Alert>
        ) : dirs.data?.enabled === false ? (
          <Alert color="yellow" variant="light">
            {dirs.data.reason ?? t("transfer.dirs.needsCookie")}
          </Alert>
        ) : (
          <Table striped highlightOnHover withTableBorder>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("transfer.dirs.columnName")}</Table.Th>
                <Table.Th>{t("transfer.dirs.columnId")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {(dirs.data?.items ?? []).map((item) => (
                <Table.Tr key={item.id}>
                  <Table.Td>
                    <UnstyledButton
                      onClick={() => setTrail([...trail, { id: item.id, name: item.name }])}
                    >
                      <Text size="sm" c="blue">
                        {item.name}
                      </Text>
                    </UnstyledButton>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" c="dimmed">
                      {item.id}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
              {dirs.data?.items.length === 0 && (
                <Table.Tr>
                  <Table.Td colSpan={2}>
                    <Text size="sm" c="dimmed">
                      {t("transfer.dirs.empty")}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        )}
      </Stack>
    </Card>
  );
}
