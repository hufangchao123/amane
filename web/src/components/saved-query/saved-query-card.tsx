import { ActionIcon, Badge, Box, Card, Checkbox, Group, Image, Stack, Text } from "@mantine/core";
import { IconDownload, IconPencil, IconPhoto, IconTrash } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import type { CSSProperties, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { SavedQueryResponse } from "@/client/types.gen";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { proxyImageUrl } from "@/lib/utils";
import { SAVED_QUERY_BADGE_COLOR, SAVED_QUERY_ENTITY_LABEL_KEY } from "@/lib/saved-query/display";

/** 缩略图尺寸: 竖版海报比例, 与媒体库卡片一致. */
const THUMB_WIDTH = 56;
const THUMB_HEIGHT = 76;

/** 预设卡左侧的配图: 取第一条结果的海报 / 头像, 没有就画占位图标. */
function SavedQueryThumbnail({ query }: { query: SavedQueryResponse }) {
  const preview = query.preview;
  const url = proxyImageUrl(preview?.image_url);
  if (url) {
    return (
      <Image
        src={url}
        alt={preview?.title ?? query.name}
        w={THUMB_WIDTH}
        h={THUMB_HEIGHT}
        radius="sm"
        fit="cover"
        loading="lazy"
      />
    );
  }
  return (
    <Box
      w={THUMB_WIDTH}
      h={THUMB_HEIGHT}
      bg="var(--mantine-color-default-hover)"
      style={{
        borderRadius: "var(--mantine-radius-sm)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flex: "0 0 auto",
      }}
    >
      <IconPhoto size={20} opacity={0.45} />
    </Box>
  );
}

const CARD_LINK_STYLE: CSSProperties = {
  flex: 1,
  minWidth: 0,
  textAlign: "left",
  textDecoration: "none",
  color: "inherit",
  display: "block",
};

/** 用 Link 承载卡片本体, 保留中键 / 修饰键新标签页与复制链接. */
function SavedQueryCardLink({
  query,
  children,
}: {
  query: SavedQueryResponse;
  children: ReactNode;
}) {
  if (query.entity === "metadata") {
    return (
      <Link to="/meta" search={{ saved_query_id: query.id }} style={CARD_LINK_STYLE}>
        {children}
      </Link>
    );
  }
  if (query.entity === "actor") {
    return (
      <Link to="/actors" search={{ saved_query_id: query.id }} style={CARD_LINK_STYLE}>
        {children}
      </Link>
    );
  }
  return (
    <Link
      to="/saved-queries/$queryId"
      params={{ queryId: String(query.id) }}
      style={CARD_LINK_STYLE}
    >
      {children}
    </Link>
  );
}

export function SavedQueryCard({
  query,
  selected,
  onToggleSelect,
  onEdit,
  onDelete,
  onDownload,
}: {
  query: SavedQueryResponse;
  selected: boolean;
  onToggleSelect: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onDownload: () => void;
}) {
  const { t, i18n } = useTranslation(["savedQueries", "common"]);

  return (
    <Card withBorder radius="md" padding="sm">
      <Group align="flex-start" wrap="nowrap" gap="xs">
        <Checkbox
          size="xs"
          mt={3}
          checked={selected}
          onChange={onToggleSelect}
          aria-label={query.name}
        />
        <SavedQueryCardLink query={query}>
          <Group align="flex-start" wrap="nowrap" gap="sm">
            <SavedQueryThumbnail query={query} />
            <Stack gap={6} style={{ minWidth: 0 }}>
              <Text fw={600} lineClamp={2} style={{ lineHeight: 1.3 }}>
                {query.name}
              </Text>
              <Group gap={6} wrap="wrap">
                <Badge size="sm" variant="light" color={SAVED_QUERY_BADGE_COLOR[query.entity]}>
                  {t(SAVED_QUERY_ENTITY_LABEL_KEY[query.entity])}
                </Badge>
                <Text size="xs" c="dimmed">
                  {t("updatedAt", {
                    time: formatRelativeTime(query.updated_at, i18n.language, t("justNow")),
                  })}
                </Text>
              </Group>
              {query.description.trim() !== "" && (
                <Text size="sm" c="dimmed" lineClamp={2}>
                  {query.description}
                </Text>
              )}
            </Stack>
          </Group>
        </SavedQueryCardLink>
        <Group gap={2} wrap="nowrap">
          <ActionIcon
            variant="subtle"
            color="gray"
            size="sm"
            aria-label={t("common:actions.edit")}
            onClick={onEdit}
          >
            <IconPencil size={15} />
          </ActionIcon>
          <ActionIcon
            variant="subtle"
            color="gray"
            size="sm"
            aria-label={t("download")}
            onClick={onDownload}
          >
            <IconDownload size={15} />
          </ActionIcon>
          <ActionIcon
            variant="subtle"
            color="red"
            size="sm"
            aria-label={t("delete")}
            onClick={onDelete}
          >
            <IconTrash size={15} />
          </ActionIcon>
        </Group>
      </Group>
    </Card>
  );
}
