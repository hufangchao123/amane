import { createFileRoute, redirect } from "@tanstack/react-router";

/** 备份产物已并入设置页的备份分段, 旧地址直接转过去. */
export const Route = createFileRoute("/backup")({
  beforeLoad: () => {
    throw redirect({ to: "/settings", search: { section: "backup" } });
  },
});
