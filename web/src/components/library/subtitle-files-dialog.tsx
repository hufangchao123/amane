import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  FileButton,
  Group,
  Loader,
  Modal,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconAlertTriangle, IconTrash, IconUpload } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch } from "@/lib/api-token";
import { confirm } from "@/lib/confirm";
import { formatFileSize } from "@/lib/utils";

interface SubtitleFileEntry {
  name: string;
  path: string;
  extension: string;
  size: number;
  language: string | null;
  modified_at: string | null;
}

interface SubtitleFilesResponse {
  media_file_id: number;
  video_path: string;
  extensions: string[];
  files: SubtitleFileEntry[];
}

interface SubtitleFilesDialogProps {
  /** 传空表示关闭. */
  mediaFileId: number | null;
  videoName?: string;
  onClose: () => void;
  /** 增删之后通知调用方刷新列表 (字幕角标会变). */
  onChanged?: () => void;
}

interface ErrorBody {
  detail?: string;
}

async function readError(resp: Response, fallback: string): Promise<string> {
  const body = await resp
    .json()
    .then((parsed: ErrorBody) => parsed.detail ?? "")
    .catch(() => "");
  return body || fallback;
}

/** 一个视频旁边的字幕清单: 列出现有的、删掉不要的、再传新的 (可自定义文件名). */
export function SubtitleFilesDialog({
  mediaFileId,
  videoName,
  onClose,
  onChanged,
}: SubtitleFilesDialogProps) {
  const { t } = useTranslation(["library", "common"]);
  const [busy, setBusy] = useState(false);
  const [customName, setCustomName] = useState("");

  const listingQuery = useQuery({
    queryKey: ["subtitles", "files", mediaFileId],
    enabled: mediaFileId !== null,
    refetchOnMount: "always",
    queryFn: async () => {
      const resp = await apiFetch(`/api/subtitles/files?media_file_id=${mediaFileId}`);
      if (!resp.ok) throw new Error(await readError(resp, t("subtitles.loadFailed")));
      return (await resp.json()) as SubtitleFilesResponse;
    },
  });

  const listing = listingQuery.data ?? null;
  const error = listingQuery.error instanceof Error ? listingQuery.error.message : null;

  async function handleUpload(file: File) {
    if (mediaFileId === null) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append("media_file_id", String(mediaFileId));
      form.append("file", file);
      if (customName.trim()) form.append("filename", customName.trim());
      const resp = await apiFetch("/api/subtitles/upload", { method: "POST", body: form });
      if (!resp.ok) {
        notifications.show({
          message: await readError(resp, t("library:upload.failed")),
          color: "red",
        });
        return;
      }
      notifications.show({ message: t("library:upload.done"), color: "teal" });
      onChanged?.();
      await listingQuery.refetch();
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(entry: SubtitleFileEntry) {
    if (mediaFileId === null) return;
    const ok = await confirm({
      title: t("subtitles.deleteTitle"),
      message: t("subtitles.deleteConfirm", { name: entry.name }),
      confirmLabel: t("subtitles.deleteAction"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append("media_file_id", String(mediaFileId));
      form.append("path", entry.path);
      const resp = await apiFetch("/api/subtitles/delete", { method: "POST", body: form });
      if (!resp.ok) {
        notifications.show({
          message: await readError(resp, t("subtitles.deleteFailed")),
          color: "red",
        });
        return;
      }
      onChanged?.();
      await listingQuery.refetch();
    } finally {
      setBusy(false);
    }
  }

  const files = listing?.files ?? [];
  const extensions = (listing?.extensions ?? []).join(",");

  return (
    <Modal
      opened={mediaFileId !== null}
      onClose={onClose}
      title={t("subtitles.title")}
      size="lg"
      closeOnClickOutside={!busy}
    >
      <Stack gap="md">
        <Text size="xs" c="dimmed" ff="monospace" truncate="end">
          {listing?.video_path ?? videoName ?? ""}
        </Text>

        {error && (
          <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />}>
            {error}
          </Alert>
        )}

        {listingQuery.isLoading ? (
          <Group justify="center" py="md">
            <Loader size="sm" />
          </Group>
        ) : files.length === 0 ? (
          <Text size="sm" c="dimmed">
            {t("subtitles.empty")}
          </Text>
        ) : (
          <Table verticalSpacing="xs" fz="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("subtitles.columns.name")}</Table.Th>
                <Table.Th w={90}>{t("subtitles.columns.language")}</Table.Th>
                <Table.Th w={110}>{t("subtitles.columns.size")}</Table.Th>
                <Table.Th w={140}>{t("subtitles.columns.modified")}</Table.Th>
                <Table.Th w={48} />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {files.map((entry) => (
                <Table.Tr key={entry.path}>
                  <Table.Td>
                    <Text size="sm" truncate="end" ff="monospace">
                      {entry.name}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {entry.language ? (
                      <Badge size="sm" variant="light">
                        {entry.language}
                      </Badge>
                    ) : (
                      <Text size="sm" c="dimmed">
                        —
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{formatFileSize(entry.size)}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" c="dimmed">
                      {entry.modified_at ? entry.modified_at.slice(0, 10) : "—"}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Tooltip label={t("subtitles.deleteAction")}>
                      <ActionIcon
                        variant="subtle"
                        color="red"
                        size="sm"
                        disabled={busy}
                        aria-label={t("subtitles.deleteAction")}
                        onClick={() => void handleDelete(entry)}
                      >
                        <IconTrash size={16} />
                      </ActionIcon>
                    </Tooltip>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}

        <Stack gap="xs">
          <TextInput
            label={t("subtitles.uploadNameLabel")}
            description={t("subtitles.uploadNameHint")}
            placeholder={t("subtitles.uploadNamePlaceholder")}
            value={customName}
            onChange={(event) => setCustomName(event.currentTarget.value)}
          />
          <Group justify="flex-end">
            <FileButton
              accept={extensions || ".srt,.ass,.ssa,.vtt,.sub"}
              onChange={(file) => {
                if (file) void handleUpload(file);
              }}
            >
              {(props) => (
                <Button
                  {...props}
                  size="xs"
                  variant="light"
                  leftSection={<IconUpload size={14} />}
                  loading={busy}
                >
                  {t("subtitles.uploadAction")}
                </Button>
              )}
            </FileButton>
          </Group>
        </Stack>
      </Stack>
    </Modal>
  );
}
