import { Button } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { submitTaskMutation, transferStatusQueryKey } from "@/client/@tanstack/react-query.gen";
import { extractErrorMessage } from "@/lib/api-error";

/** 「立即中转」只入队任务: 搬移在任务里跑, 页面不等它结束. */
export function RunTransferButton() {
  const { t } = useTranslation(["ops", "common"]);
  const queryClient = useQueryClient();
  const run = useMutation({
    ...submitTaskMutation(),
    onSuccess: () => {
      notifications.show({ message: t("ops:transfer.runQueued"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: transferStatusQueryKey() });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  return (
    <Button loading={run.isPending} onClick={() => run.mutate({ body: { type: "transfer_115" } })}>
      {t("ops:transfer.runNow")}
    </Button>
  );
}
