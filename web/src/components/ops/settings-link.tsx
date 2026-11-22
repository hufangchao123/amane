import { Button } from "@mantine/core";
import { IconSettings } from "@tabler/icons-react";
import { useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

/** 页面只报现状, 参数在设置页; 给一条直达该配置段的入口. */
export function SettingsLink({ section }: { section: string }) {
  const { t } = useTranslation("ops");
  const navigate = useNavigate();
  return (
    <Button
      onClick={() => void navigate({ to: "/settings", search: { section } })}
      leftSection={<IconSettings size={16} />}
      variant="light"
      size="xs"
    >
      {t("configure")}
    </Button>
  );
}
