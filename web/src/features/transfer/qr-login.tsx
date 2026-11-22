import { Alert, Badge, Button, Card, Group, Image, Select, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { TransferLoginClient, TransferQrTicketResponse } from "@/client";
import {
  pollTransferQrcodeOptions,
  startTransferQrcodeMutation,
  transferStatusQueryKey,
} from "@/client/@tanstack/react-query.gen";
import { extractErrorMessage } from "@/lib/api-error";

type Translate = TFunction<["ops"]>;

const CLIENTS: TransferLoginClient[] = ["web", "android", "ios", "windows", "mac", "linux", "tv"];

/** 客户端名按固定顺序取文案: 模板键会被抽取器当成占位键. */
function clientLabel(t: Translate, client: TransferLoginClient): string {
  switch (client) {
    case "android":
      return t("transfer.qr.clients.android");
    case "ios":
      return t("transfer.qr.clients.ios");
    case "windows":
      return t("transfer.qr.clients.windows");
    case "mac":
      return t("transfer.qr.clients.mac");
    case "linux":
      return t("transfer.qr.clients.linux");
    case "tv":
      return t("transfer.qr.clients.tv");
    default:
      return t("transfer.qr.clients.web");
  }
}

function statusLabel(t: Translate, status: string): string {
  switch (status) {
    case "scanned":
      return t("transfer.qr.scanned");
    case "confirmed":
      return t("transfer.qr.confirmed");
    case "expired":
      return t("transfer.qr.expired");
    default:
      return t("transfer.qr.waiting");
  }
}

/**
 * 扫码登录: 换一张二维码, 轮询到确认后由后端把 Cookie 写进配置.
 * 图片走后端转发, 浏览器不直连 115; 界面永远不回显 Cookie.
 */
export function QrLogin() {
  const { t } = useTranslation("ops");
  const queryClient = useQueryClient();
  const [client, setClient] = useState<TransferLoginClient>("web");
  const [ticket, setTicket] = useState<TransferQrTicketResponse | null>(null);

  const start = useMutation({
    ...startTransferQrcodeMutation(),
    onSuccess: (data: TransferQrTicketResponse) => setTicket(data),
    onError: (error) =>
      notifications.show({
        message: extractErrorMessage(error, t("transfer.qr.startFailed")),
        color: "red",
      }),
  });

  const poll = useQuery({
    ...pollTransferQrcodeOptions({
      query: {
        client: ticket?.client ?? client,
        uid: ticket?.uid ?? "",
        time: ticket?.time ?? "",
        sign: ticket?.sign ?? "",
      },
    }),
    enabled: ticket !== null && ticket.uid !== "",
    refetchInterval: (query) => (query.state.data?.status === "confirmed" ? false : 2000),
  });

  const status = poll.data?.status ?? (ticket === null ? null : "waiting");
  const saved = poll.data?.saved === true;

  useEffect(() => {
    if (!saved) return;
    void queryClient.invalidateQueries({ queryKey: transferStatusQueryKey() });
    notifications.show({ message: t("transfer.qr.saved"), color: "teal" });
  }, [saved, queryClient, t]);

  return (
    <Card withBorder>
      <Stack gap="sm">
        <Group justify="space-between">
          <Text fw={600}>{t("transfer.qr.title")}</Text>
          {status !== null && <Badge variant="light">{statusLabel(t, status)}</Badge>}
        </Group>
        <Text size="xs" c="dimmed">
          {t("transfer.qr.hint")}
        </Text>
        <Group align="end" gap="sm">
          <Select
            label={t("transfer.qr.client")}
            data={CLIENTS.map((value) => ({ value, label: clientLabel(t, value) }))}
            value={client}
            onChange={(value) => setClient((value ?? "web") as TransferLoginClient)}
            allowDeselect={false}
            w={200}
          />
          <Button loading={start.isPending} onClick={() => start.mutate({ body: { client } })}>
            {ticket === null || saved ? t("transfer.qr.start") : t("transfer.qr.refresh")}
          </Button>
        </Group>
        {ticket !== null && !saved && (
          <Stack gap="xs" align="center">
            <Image
              src={`/api/transfer/qrcode/image?client=${ticket.client}&uid=${ticket.uid}`}
              alt={t("transfer.qr.title")}
              w={180}
              h={180}
            />
            <Text size="xs" c="dimmed">
              {t("transfer.qr.expires", { seconds: ticket.expires_in })}
            </Text>
          </Stack>
        )}
        {poll.isError && (
          <Alert color="red" variant="light">
            {t("transfer.qr.pollFailed")}
          </Alert>
        )}
      </Stack>
    </Card>
  );
}
