/** 非 null 对象守卫 (含数组 / Date 等); 用于边界解析而非 `as Record`. */
export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

/**
 * 将外链图片 URL 包装为后端代理 URL, 绕过浏览器 CORS / 防盗链限制.
 * 仅 http(s) 协议经代理, 其他 (data:, blob:, 相对路径) 原样返回.
 */
export function proxyImageUrl(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  if (/^https?:\/\//i.test(url)) {
    return `/api/resources/proxy?url=${encodeURIComponent(url)}`;
  }
  return url;
}

/** 空值 / 无效值的统一占位: 全角破折号, 与页面里其他空态标记一致. */
const EMPTY_PLACEHOLDER = "—";

/** 解析时间戳; 空值与无法解析的值一律视为无效. */
function toValidDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** 本地化日期 (不含时间); 空值 / 无效值给占位符. */
export function formatDate(value: string | null | undefined, locale: string): string {
  const date = toValidDate(value);
  return date ? date.toLocaleDateString(locale) : EMPTY_PLACEHOLDER;
}

/** 本地化日期 + 时间; 空值 / 无效值给占位符. */
export function formatDateTime(value: string | null | undefined, locale: string): string {
  const date = toValidDate(value);
  return date ? date.toLocaleString(locale) : EMPTY_PLACEHOLDER;
}

const FILE_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes == null) return EMPTY_PLACEHOLDER;
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < FILE_SIZE_UNITS.length - 1) {
    value /= 1024;
    unitIndex++;
  }
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${FILE_SIZE_UNITS[unitIndex]}`;
}
