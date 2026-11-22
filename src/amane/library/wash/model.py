"""洗版的数据结构与择一算法.

本模块只做纯计算: 分组、排序、挑出保留者. 磁盘与数据库在 ``collect`` / ``trash`` / ``store``
里, 这样同一套「同组」判定既能被扫描任务用, 也能被只读的候选列表用, 测试可以完全脱离文件
系统跑表驱动用例.

## 「同组」的定义

``group_key`` = 库 + 番号 (+ 分段 + 有码/无码标记). 四条理由:

* 库: 不同库意味着不同的整理模板和不同的磁盘, 跨库比较既没有意义, 也无法一次回滚;
* 番号: 只有指向同一部作品的两份文件才算重复;
* 分段 (CD1/CD2): 是同一部片的不同部分, 互相回收会直接丢内容;
* 有码/无码: 是两次不同的发行, 画质差异来自版本而不是压制, 无码版不能被当成「更差的一份」扔掉.
  未标记 (``mosaic is None``) 与已标记不合并: 标记缺失时无法判断两者是否同一版本, 保守起来
  宁可不洗, 也不要洗错.

本地与 STRM 也分开 (落到 ``wash_records.kind``), 因为 STRM 只是一行指针: 它的体积是几百字节,
码率无从谈起, 拿去和本地视频比大小正是二次审计的 P0-7.

## 保留谁 (本地)

按下面的顺序比较, 先分出高下的为准 (优先级从高到低):

1. **分辨率档位**: ``DEFINITION_VALUES`` 下标小者优先 (8K → 4K → 1440p → 1080p → 720p → 480p),
   未知档位排在最后. 分辨率是观感的第一决定因素, 与编码效率无关, 因此排在码率之前.
2. **码率 = 体积 / 时长**: 同档位下码率越高, 单位时间装进去的信息越多. 时长未知 (缺失或 0) 时
   这一档取中性值 0, 排在有时长的候选之后 —— 拿不出时长就没法证明自己码率更高, 不拿体积冒充.
3. **体积**: 前面都相同时留大的, 主要用来兜住「时长未知但大即完整」的常见情形.
4. **容器优先级**: mkv > mp4 > m4v > mov > wmv > ts > avi > flv > iso > 其它. 容器不影响画质,
   但 mkv 能装多音轨 / 多字幕轨, 且不需要 faststart; 整理的模板也以它最完整.
5. **路径字典序**: 纯粹为了决定性 —— 同样的输入每次必须选出同一份, 否则重复执行会来回搬文件.

## 保留谁 (STRM)

STRM 组只按容器优先级 → 路径字典序比较, 不看体积 / 码率 / 分辨率档位: 指针本身没有画质,
写进文件名的档位说的是云端那份文件, 拿它排序等于信一个没有校验过的名字. 洗版的目的是让
Emby 里同一部片只剩一个入口, 谁留下并不重要, 重要的是一定要确定.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...db.models import WashRecordKind
from ...enums import StrmPrefer, WashPrefer
from ...parsing import DEFINITION_VALUES, canonical_definition

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ...parsing import Mosaic

#: 容器优先级: 越靠前越优先保留. 不在表里的扩展名排最后 (按字典序区分).
CONTAINER_PRIORITY: tuple[str, ...] = (
    ".mkv",
    ".mp4",
    ".m4v",
    ".mov",
    ".wmv",
    ".ts",
    ".avi",
    ".flv",
    ".iso",
    ".rmvb",
    ".mpg",
    ".mpeg",
)

_DEFINITION_ORDER: dict[str, int] = {value: index for index, value in enumerate(DEFINITION_VALUES)}
_UNKNOWN_DEFINITION: int = len(_DEFINITION_ORDER)


@dataclass(frozen=True, slots=True)
class WashEntry:
    """一份参与比较的文件 (库内的字面路径).

    ``size`` 对本地文件是磁盘实况字节, 对 STRM 恒为 0 —— 指针的体积没有意义, 保留字段只是让
    记录行带上一个可以求和的数.
    """

    path: str
    kind: WashRecordKind
    library_id: int
    number: str
    size: int = 0
    duration: float | None = None
    definition: str | None = None
    mosaic: Mosaic | None = None
    cd: int | None = None
    mtime: float | None = None


@dataclass(frozen=True, slots=True)
class WashGroup:
    """一个可洗的分组: 留一份, 其余是落选者."""

    group_key: str
    kind: WashRecordKind
    library_id: int
    keep: WashEntry
    drops: tuple[WashEntry, ...]

    @property
    def bytes_freed(self) -> int:
        """洗掉这一组能空出来的字节数 (STRM 组恒为 0)."""
        return sum(entry.size for entry in self.drops)


def group_key_of(entry: WashEntry) -> str:
    """同组判定键: 库 + 番号 (+ 分段 + 有码标记), 见模块文档."""
    parts = [f"lib{entry.library_id}", entry.number.strip().upper()]
    if entry.cd is not None:
        parts.append(f"cd{entry.cd}")
    if entry.mosaic is not None:
        parts.append(entry.mosaic.value)
    return ":".join(parts)


def library_id_of(group_key: str) -> int | None:
    """从 ``group_key`` 反解库 id; 还原要据此找回库根, 解不出返回 None."""
    prefix, _, _rest = group_key.partition(":")
    if not prefix.startswith("lib"):
        return None
    try:
        return int(prefix[3:])
    except ValueError:
        return None


def definition_rank(definition: str | None) -> int:
    """分辨率档位序号, 越小越好; 未知值排在已知档位之后."""
    if definition is None:
        return _UNKNOWN_DEFINITION
    canonical = canonical_definition(definition)
    if canonical is None:
        return _UNKNOWN_DEFINITION
    return _DEFINITION_ORDER.get(canonical, _UNKNOWN_DEFINITION)


def container_rank(path: str | Path) -> int:
    """容器优先级序号, 越小越好."""
    suffix = Path(path).suffix.casefold()
    try:
        return CONTAINER_PRIORITY.index(suffix)
    except ValueError:
        return len(CONTAINER_PRIORITY)


def local_sort_key(entry: WashEntry, prefer: WashPrefer = WashPrefer.QUALITY) -> tuple[int, float, int, int, str]:
    """本地组的排序键 (升序即优先级从高到低).

    默认的五档见模块文档; 其余三种策略只按一个维度比较, 再用容器与路径做决定性兜底 ——
    用户选了「按体积」就是要体积最大的那份, 不能又被分辨率翻盘.
    """
    container = container_rank(entry.path)
    if prefer is WashPrefer.LARGEST:
        return (0, 0.0, -entry.size, container, entry.path)
    if prefer is WashPrefer.NEWEST:
        return (0, -(entry.mtime if entry.mtime is not None else 0.0), 0, container, entry.path)
    if prefer is WashPrefer.OLDEST:
        # 缺少修改时间的排在最后: 拿不出时间就不该被当成「更旧」.
        age = entry.mtime if entry.mtime is not None else float("inf")
        return (0, age, 0, container, entry.path)
    bitrate = entry.size / entry.duration if entry.duration is not None and entry.duration > 0 else 0.0
    return (definition_rank(entry.definition), -bitrate, -entry.size, container, entry.path)


def strm_sort_key(entry: WashEntry, prefer: StrmPrefer = StrmPrefer.PATH) -> tuple[int, float, int, int, str]:
    """STRM 组的排序键 (升序即优先级从高到低).

    默认只比容器与路径: 指针本身没有画质与体积, 写进文件名的档位说的是云端那份文件, 未经校验.
    用户显式选了按文件名档位时才用它 —— 那等于接受「信名字」这个前提. 体积与码率永远不参与.
    """
    container = container_rank(entry.path)
    if prefer is StrmPrefer.DEFINITION:
        return (definition_rank(entry.definition), 0.0, 0, container, entry.path)
    if prefer is StrmPrefer.NEWEST:
        return (0, -(entry.mtime if entry.mtime is not None else 0.0), 0, container, entry.path)
    if prefer is StrmPrefer.OLDEST:
        age = entry.mtime if entry.mtime is not None else float("inf")
        return (0, age, 0, container, entry.path)
    return (0, 0.0, 0, container, entry.path)


def sort_key(
    entry: WashEntry, prefer: WashPrefer = WashPrefer.QUALITY, strm_prefer: StrmPrefer = StrmPrefer.PATH
) -> tuple[int, float, int, int, str]:
    """按条目的 kind 选排序键; 本地与 STRM 各有一套策略, 调用方须保证同一组 kind 一致."""
    if entry.kind is WashRecordKind.STRM:
        return strm_sort_key(entry, strm_prefer)
    return local_sort_key(entry, prefer)


def build_groups(
    entries: Iterable[WashEntry],
    *,
    min_size_bytes: int,
    prefer: WashPrefer = WashPrefer.QUALITY,
    strm_prefer: StrmPrefer = StrmPrefer.PATH,
) -> list[WashGroup]:
    """按 ``group_key`` 归并并选出保留者, 只返回至少两份候选的组.

    ``min_size_bytes`` 只作用于本地文件: 低于它的多半是样片 / 花絮 / 残片, 回收收益小而动它
    有风险. STRM 不参与体积过滤 —— 见模块文档的 P0-7.
    """
    buckets: dict[tuple[WashRecordKind, str], list[WashEntry]] = {}
    for entry in entries:
        if not entry.number.strip():
            # 番号解析不出来的文件没有分组依据, 不参与洗版.
            continue
        if entry.kind is WashRecordKind.LOCAL and entry.size < min_size_bytes:
            continue
        buckets.setdefault((entry.kind, group_key_of(entry)), []).append(entry)

    groups: list[WashGroup] = []
    for (kind, key), members in buckets.items():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda entry: sort_key(entry, prefer, strm_prefer))
        groups.append(
            WashGroup(
                group_key=key,
                kind=kind,
                library_id=ordered[0].library_id,
                keep=ordered[0],
                drops=tuple(ordered[1:]),
            )
        )
    # 固定顺序: 任务每次跑出来的组顺序一致, 便于对数与测试.
    groups.sort(key=lambda group: (group.kind.value, group.group_key))
    return groups
