"""只读 SQL 沙箱 - 单条查询, 超时, 只读连接, 解析层授权."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

# 安全边界由数据库层构成: 关键词黑名单这类词法校验既冗余又会误伤字面量.
# - ``mode=ro`` + ``PRAGMA query_only``: SQLite 拒绝主库与临时表的一切写语句;
# - ``sqlite3.execute`` 硬性单语句, 多语句直接报错;
# - authorizer (_readonly_authorizer): 解析层拒绝 ATTACH/DETACH、除 ``query_only=ON``
#   之外的 PRAGMA, 以及敏感表上的读取. ``mode=ro`` 只约束 main 库, ATTACH 挂载的库
#   默认以读写打开, VACUUM INTO 内部也经 ATTACH 授权动作 — 这两者是只读连接本身无法
#   拦截的, 必须由 authorizer 拦截.
# 词法层只做一件不涉安全的事: 剥注释 (``strip_sql_comments``), 因为把语句包成子查询时
# 末尾的行注释会把右括号一起注释掉.

#: 沙箱拒绝读取的表: 内容是运行状态与凭据, 不是影片数据, 与「用 SQL 看库」这件事无关.
_SENSITIVE_TABLES = frozenset(
    {
        # 订阅地址可能内嵌站点密钥, 且订阅的增删改在 feed-ops 里另有审批走法.
        "feeds",
        # 通知载荷含渠道地址与正文, 助理不需要用 SQL 读取已发出的通知.
        "notify_records",
        # 留痕表记客户端 IP 与请求路径, 属运维元数据; 可读等于给出窥探访问日志的旁路.
        "audit_records",
        # 会话标题与状态, 读取会暴露其他会话在聊什么, 且与影片数据无关.
        "agent_sessions",
    }
)
"""黑名单按「跨用户可见的运行状态」取, 因此表的取舍理由逐表写下.

与之相对, ``tasks`` / ``schedules`` / ``task_links`` 保持可读: 任务与定时任务的内容本来就由
``list_schedules`` 这类工具直出, 排查故障时用 SQL 聚合队列状态是正当用途; ``wash_choices`` 只记
留版标记, 同样是助理看得到的领域数据. 黑名单只约束读取, 写操作由只读连接与 ``query_only`` 兜住.
"""


def _readonly_authorizer(action: int, arg1: str | None, arg2: str | int | None, *_rest: object) -> int:
    """解析层只读授权: 拒绝 ATTACH/DETACH、敏感表读取, 以及 ``query_only=ON`` 之外的 PRAGMA.

    ``query_only`` 只放行 ``=ON``: 它是沙箱自身建立的连接态, 而 ``PRAGMA query_only=OFF``
    会把这层写保护关掉, 等于把只读连接换回可写连接. 其余 PRAGMA (含 journal_mode /
    schema_version 等写文件类) 一律拒绝.
    """
    del _rest  # 回调签名由 sqlite3 C 层固定, 多余位置参数无用
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        if arg1 != "query_only" or str(arg2).strip().upper() not in {"ON", "1"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and (arg1 or "").strip('"`[]').lower() in _SENSITIVE_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


#: 引号标识符的收尾字符; 引号内双写表示转义, 与 SQLite 一致.
_QUOTE_CLOSERS = {"'": "'", '"': '"', "`": "`", "[": "]"}


def strip_sql_comments(sql: str) -> str:
    """剥掉行注释与块注释, 字面量与引号标识符里的原文原样保留.

    注释剥离不承担防注入职责: SQLite 解析器本就跳过注释, 注释后面藏不住表名或写语句.
    需要它是因为 :func:`as_id_subquery_sql` 用括号把整条语句包成子查询 — 语句末尾的行注释
    会把右括号一起注释掉, 变成语法错误; 预置查询由人编写, 结尾留一句注释很常见.
    """
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            if newline < 0:
                break
            index = newline  # 保留换行: 前后两行不能粘成一行
            continue
        if char == "/" and sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise SqlSandboxError("SQL 注释未闭合: ``/*`` 缺少 ``*/``")
            out.append(" ")  # 补空格: a/*x*/b 不能粘成 ab
            index = end + 2
            continue
        closer = _QUOTE_CLOSERS.get(char)
        if closer is None:
            out.append(char)
            index += 1
            continue
        start = index
        index += 1
        while index < length:
            if sql[index] == closer:
                if index + 1 < length and sql[index + 1] == closer:
                    index += 2  # 双写续跳, 不能当成收尾引号
                    continue
                index += 1
                break
            index += 1
        out.append(sql[start:index])  # 字面量整体照抄, 里面出现 -- 或 /* 也不动
    return "".join(out)


def _normalize_sql(sql: str) -> str:
    """剥注释, 去首尾空白与末尾分号."""
    return strip_sql_comments(sql).strip().rstrip(";").strip()


class SqlSandboxError(Exception):
    """SQL 执行失败 (含授权拒绝)."""


class SqlTimeoutError(SqlSandboxError):
    """查询超过超时."""


class SqlNeedsApproval(SqlSandboxError):
    """需要用户批准放宽超时后才能执行."""

    def __init__(self, sql: str, reason: str = "allow_slow") -> None:
        super().__init__(reason)
        self.sql = sql
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SqlResult:
    """只读查询结果; ``row_count`` 为 -1 表示截断 (实际至少已取这些行)."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: float


def as_id_subquery_sql(sql: str) -> str:
    """把 SQL 包成 ``SELECT id FROM (...) AS _sq``, 供列表 WHERE id IN (子查询).

    子查询语法只接受 SELECT/WITH, 非查询语句在语法层即失败 — 包装本身即约束.
    包装前剥注释: 语句末尾的行注释会把包装用的右括号一并注释掉, 变成语法错误.
    """
    return f"SELECT id FROM ({_normalize_sql(sql)}) AS _sq"


_PROGRESS_STEPS = 10_000
"""进度回调的间隔 (虚拟机指令数): 期限精度约在毫秒级, 实测不拖慢查询, 也不影响事件循环."""


def _is_interrupted(exc: sqlite3.Error) -> bool:
    """进度回调中断的语句报 SQLITE_INTERRUPT."""
    return isinstance(exc, sqlite3.OperationalError) and exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT


class ReadonlySqlSandbox:
    """对主库文件开只读 URI 连接执行查询."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    async def execute(
        self,
        sql: str,
        *,
        timeout_ms: int,
        allow_slow: bool = False,
        approved: bool = False,
        max_rows: int | None = None,
    ) -> SqlResult:
        """执行只读 SQL.

        ``allow_slow`` 且未 ``approved`` 时不执行, 抛 ``SqlNeedsApproval``.
        ``approved`` 只跳过这道闸门, 超时仍取 ``timeout_ms`` — 它不随批准自动放宽.
        """
        if allow_slow and not approved:
            raise SqlNeedsApproval(sql)

        normalized = _normalize_sql(sql)
        if not normalized:
            raise SqlSandboxError("SQL 为空")
        uri = self._db_path.resolve().as_uri() + "?mode=ro"

        try:
            return await self._run(uri, normalized, timeout_ms=timeout_ms, max_rows=max_rows)
        except sqlite3.Error as exc:
            # 列名/表名错误等必须变成工具可回收的 SqlSandboxError,
            # 否则会冒泡成 SSE StreamError 打断整轮 (模型无法自行修改 SQL 重试).
            raise SqlSandboxError(str(exc)) from exc

    async def _run(self, uri: str, sql: str, *, timeout_ms: int, max_rows: int | None) -> SqlResult:
        """超时只计执行与取行, 不计建连: 建连耗时随磁盘负载波动, 计入会让轻量查询被误判超时.

        查询在 aiosqlite 的专属线程里运行, 取消协程停不下它; 只能由 SQLite 每隔
        ``_PROGRESS_STEPS`` 条虚拟机指令回调一次, 越过期限即中断当前语句.
        """
        async with aiosqlite.connect(uri, uri=True) as conn:
            await conn.execute("PRAGMA query_only = ON")
            await conn.set_authorizer(_readonly_authorizer)
            conn.row_factory = aiosqlite.Row
            started = time.perf_counter()
            deadline = started + max(timeout_ms, 1) / 1000.0
            await conn.set_progress_handler(lambda: time.perf_counter() > deadline, _PROGRESS_STEPS)
            try:
                cursor = await conn.execute(sql)
                try:
                    description = cursor.description or ()
                    columns = [col[0] for col in description]
                    if max_rows is None:
                        raw_rows = await cursor.fetchall()
                    else:
                        raw_rows = await cursor.fetchmany(max_rows)
                    rows = [list(row) for row in raw_rows]
                    # row_count: 若截断则至少为已取行数; 全量时等于 len(rows)
                    row_count = len(rows)
                    if max_rows is not None and len(rows) == max_rows:
                        # 再取一行探测是否还有更多 - 不把探测行并入结果
                        more = list(await cursor.fetchmany(1))
                        if more:
                            # 未知精确总数时用 -1 表示"至少 max_rows"
                            row_count = -1
                finally:
                    await cursor.close()
            except sqlite3.Error as exc:
                if not _is_interrupted(exc):
                    raise
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                raise SqlTimeoutError(f"查询超时 ({elapsed_ms:.0f}ms > {timeout_ms}ms)") from exc
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return SqlResult(columns=columns, rows=rows, row_count=row_count, elapsed_ms=elapsed_ms)
