import type { TransferItemState } from "@/client/types.gen";

/** 表格里的状态徽标配色: 搬走是完成, 跳过是可解释的结论, 失败要显眼. */
export const STATE_COLORS: Record<TransferItemState, string> = {
  pending: "gray",
  moved: "teal",
  skipped: "yellow",
  failed: "red",
};

/** 状态卡与徽标共用的顺序: 从"还没动"到"出了问题". */
export const COUNTED_STATES = [
  "pending",
  "moved",
  "skipped",
  "failed",
] as const satisfies readonly TransferItemState[];
