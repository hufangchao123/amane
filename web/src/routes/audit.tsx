import { createFileRoute, redirect } from "@tanstack/react-router";

/** 审计面板已并入设置页, 旧地址直接转过去. */
export const Route = createFileRoute("/audit")({
  beforeLoad: () => {
    throw redirect({ to: "/settings", search: { section: "audit" } });
  },
});
