/** `Select` 的 `onChange` 只会给字符串, 用它在选项表里收窄回字面量类型.
 *
 * 生成的分页参数用的是字面量联合, 而 Mantine 的回调签名是 `string | null`;
 * 与其断言, 不如按选项表校验一次: 选项表是唯一真源, 未知值落到默认值.
 */
export function pickOption<T extends string>(
  options: readonly T[],
  value: string | null,
  fallback: T,
): T {
  return options.find((item) => item === value) ?? fallback;
}

/** 同上, 但「不选」是合法状态 —— 可清空的筛选项回到 `null` 表示不过滤. */
export function pickOptionOrNull<T extends string>(
  options: readonly T[],
  value: string | null,
): T | null {
  return options.find((item) => item === value) ?? null;
}
