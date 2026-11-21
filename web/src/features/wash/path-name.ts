/** 路径末段, 只给表格显示用.
 *
 * 洗版记录里的路径都来自库内, 分隔符固定是 `/`; 不用 node 的 path 模块, 免得把服务器端语义带进浏览器包.
 */
export function pathName(path: string): string {
  const name = path.split("/").at(-1);
  return name && name.length > 0 ? name : path;
}

/** 分组键 → 番号, 表格里再显示一次库 id 是噪音.
 *
 * 分组键形如 `lib3:ABC-123:cd2:uncensored`, 第一段是库 id, 第二段才是番号.
 */
export function groupNumber(groupKey: string): string {
  return groupKey.split(":").at(1) ?? groupKey;
}
