"""按需读取 ``app.log`` (含轮转文件) 的末尾若干行, 解析为与 WebSocket LOG 事件同形的条目.

日志页的实时列表只存在浏览器内存里, 刷新即丢. 这个模块给页面一个回填历史的口子:
从最新的 ``app.log`` 往前读, 不够再读 ``app.log.1`` / ``app.log.2`` …, 直到凑够调用方要的条数.
"""

from __future__ import annotations

from pathlib import Path

from .models import TaskLogPage
from .task_log import _parse_line, _read_tail_lines

MAIN_LOG_NAME = "app.log"


def log_files(log_dir: Path) -> list[Path]:
    """按「新 → 旧」返回 app.log 与它的轮转文件.

    ``RotatingFileHandler`` 的编号越小越新, 所以 ``app.log`` 之后是 ``.1``、``.2`` …
    """
    main = log_dir / MAIN_LOG_NAME
    backups: list[tuple[int, Path]] = []
    for path in log_dir.glob(f"{MAIN_LOG_NAME}.*"):
        suffix = path.name.removeprefix(f"{MAIN_LOG_NAME}.")
        if suffix.isdigit():
            backups.append((int(suffix), path))
    backups.sort()
    return [main, *(path for _, path in backups)]


def read_app_log(log_dir: Path, *, limit: int) -> TaskLogPage:
    """运行日志的尾部 ``limit`` 行. 文件不存在或全被轮转掉时返回空页."""
    if limit < 1:
        raise ValueError("limit must be positive")
    collected: list[bytes] = []
    truncated = False
    for path in log_files(log_dir):
        remaining = limit - len(collected)
        if remaining <= 0:
            truncated = True
            break
        if not path.is_file():
            continue
        try:
            lines, more = _read_tail_lines(path, remaining)
        except FileNotFoundError:
            continue
        collected = [*lines, *collected]
        truncated = truncated or more
    entries = [entry for line in collected if (entry := _parse_line(line)) is not None]
    return TaskLogPage(entries=entries, truncated=truncated)
