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
2. **配置里排定的其它维度** (来自 ``wash.prefer``): 字幕、体积、时间等, 按顺序比. 它们在档位
   相同之后、码率与体积之前生效 —— 「同档位时带中文字幕的那份更值得留」是常见取舍.
3. **码率 = 体积 / 时长**: 前面都不分高下时才比码率, 单位时间装进去的信息越多越好. 时长未知
   (缺失或 0) 时这一档取中性值 0, 排在有时长的候选之后 —— 拿不出时长就没法证明自己码率更高.
4. **体积**: 还相同就留大的, 兜住「时长未知但大即完整」的常见情形.
5. **容器优先级**: mkv > mp4 > m4v > mov > wmv > ts > avi > flv > iso > 其它. 容器不影响画质,
   但 mkv 能装多音轨 / 多字幕轨, 且不需要 faststart; 整理的模板也以它最完整.
6. **路径字典序**: 纯粹为了决定性 —— 同样的输入每次必须选出同一份, 否则重复执行会来回搬文件.

## 保留谁 (STRM)

STRM 组只按容器优先级 → 路径字典序比较, 不看体积 / 码率 / 分辨率档位: 指针本身没有画质,
写进文件名的档位说的是云端那份文件, 拿它排序等于信一个没有校验过的名字. 洗版的目的是让
Emby 里同一部片只剩一个入口, 谁留下并不重要, 重要的是一定要确定.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...db.models import WashRecordKind
from ...enums import StrmPrefer, WashPrefer
from ...parsing import DEFINITION_VALUES, canonical_definition

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

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

#: 字幕维度取值: 文件名带中文字幕标记最好, 其次有外挂字幕, 最后是都没有.
CHINESE_SUBTITLE_RANK: int = 0
EXTERNAL_SUBTITLE_RANK: int = 1
NO_SUBTITLE_RANK: int = 2

#: 中文字幕的文件名标记. ``-c`` 单独成段才算 (常见于「番号-C」这种中文字幕版命名),
#: 所以要连边界一起匹配, 否则 ``-ch``/``-cd2`` 之类会被误判.
_CHINESE_SUBTITLE_PATTERN = re.compile(r"(?:^|[-._\s\[\]()])c(?:$|[-._\s\[\]()])", re.IGNORECASE)
_CHINESE_SUBTITLE_TOKENS = ("中字", "中文", "字幕", "简中", "繁中", "chs", "cht", "chinese")


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
    subtitle_rank: int = NO_SUBTITLE_RANK
    """字幕维度: 越小越好, 见 ``subtitle_rank_of``; 缺省按「没有字幕」算."""


@dataclass(frozen=True, slots=True)
class WashGroup:
    """一个可洗的分组: 留一份, 其余是落选者."""

    group_key: str
    kind: WashRecordKind
    library_id: int
    keep: WashEntry
    drops: tuple[WashEntry, ...]
    ignored: bool = False
    """使用者选了「都留着」: 组仍在列表里 (可以撤销), 但没有落选者、不计体积."""

    @property
    def bytes_freed(self) -> int:
        """洗掉这一组能空出来的字节数 (STRM 组恒为 0)."""
        return sum(entry.size for entry in self.drops)


#: 同一番号下的分卷/合集/花絮关键词. 命中即单独成组, 不与正片互洗.
_PART_TOKENS = (
    "前編",
    "後編",
    "前篇",
    "后篇",
    "上巻",
    "下巻",
    "上卷",
    "下卷",
    "総集編",
    "合集",
    "全集",
    "特典",
    "花絮",
)


def part_of(entry: WashEntry) -> str | None:
    """从文件名里认出分卷/合集/花絮标记; 认不出返回 None."""
    stem = Path(entry.path).stem.casefold()
    for token in _PART_TOKENS:
        if token in stem:
            return token
    return None


def group_key_of(entry: WashEntry) -> str:
    """同组判定键: 库 + 番号 (+ 分段 + 有码标记), 见模块文档."""
    parts = [f"lib{entry.library_id}", entry.number.strip().upper()]
    if entry.cd is not None:
        parts.append(f"cd{entry.cd}")
    part = part_of(entry)
    if part is not None:
        parts.append(f"part{part}")
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


def is_unambiguous(group: WashGroup, *, min_gain_percent: int) -> bool:
    """判断这一组的取舍是否「没有争议」, 供自动执行筛选用.

    两条自动准入, 都要求保留者在某个维度上**严格更好**, 否则一律留给人看:

    * 分辨率档位不同: 保留者的档位更优即通过 —— 4K 与 1080p 之间不存在「看走眼」;
    * 档位相同: 两边时长都可读 (否则码率无从判断, 也不该拿体积冒充), 且保留者比每一份落选者
      大出 ``min_gain_percent`` 个百分点 —— 体积接近时差异可能来自压制风格、音轨或字幕,
      那不是自动该决定的事.

    STRM 组恒为 False: 指针没有画质, 「留哪一份定义」牵扯播放器兼容与云端来源, 由人挑.
    """
    if group.kind is not WashRecordKind.LOCAL or not group.drops:
        return False
    keep = group.keep
    if any(drop.subtitle_rank < keep.subtitle_rank for drop in group.drops):
        # 落选者带着更好的字幕: 「更清晰」不等于「更该留」, 这种取舍交给使用者.
        return False
    keep_rank = definition_rank(keep.definition)
    if any(definition_rank(drop.definition) != keep_rank for drop in group.drops):
        return all(keep_rank < definition_rank(drop.definition) for drop in group.drops)
    if keep.duration is None or keep.duration <= 0:
        return False
    if any(drop.duration is None or drop.duration <= 0 for drop in group.drops):
        return False
    return all(drop.size > 0 and (keep.size - drop.size) * 100 >= drop.size * min_gain_percent for drop in group.drops)


def subtitle_rank_of(path: str | Path, *, has_subtitle_in_name: bool = False, has_external: bool = False) -> int:
    """一份文件的字幕维度取值: 越小越好.

    「带中文字幕」比「有字幕」更有意义 —— 库里绝大多数作品缺的是中文, 一份英文字幕不能顶替.
    文件名里的 ``-C``/中字/简中那类标记最先看, 其次才是同目录有没有外挂字幕.
    """
    stem = Path(path).stem.casefold()
    if _CHINESE_SUBTITLE_PATTERN.search(stem) or any(token in stem for token in _CHINESE_SUBTITLE_TOKENS):
        return CHINESE_SUBTITLE_RANK
    if has_external or has_subtitle_in_name:
        return EXTERNAL_SUBTITLE_RANK
    return NO_SUBTITLE_RANK


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


def _prefer_key(entry: WashEntry, prefer: WashPrefer) -> tuple[float, ...]:
    """一个比较维度的键: 元组内部仍是「先比第一项」.

    ``quality`` 在这里只出**分辨率档位**一项: 档位不同是观感上的硬差距, 先分出来; 同档位之后
    码率与体积反而是次要的 —— 它们让位给使用者排在其后的维度 (比如字幕), 由
    :func:`_quality_tail` 在末尾兜底. 其余维度各出一项.
    """
    if prefer is WashPrefer.SUBTITLE:
        return (float(entry.subtitle_rank),)
    if prefer is WashPrefer.LARGEST:
        return (-float(entry.size),)
    if prefer is WashPrefer.NEWEST:
        return (-(entry.mtime if entry.mtime is not None else 0.0),)
    if prefer is WashPrefer.OLDEST:
        # 缺少修改时间的排在最后: 拿不出时间就不该被当成「更旧」.
        age = entry.mtime if entry.mtime is not None else float("inf")
        return (age,)
    return (float(definition_rank(entry.definition)),)


def _quality_tail(entry: WashEntry) -> tuple[float, ...]:
    """画质的后两档 (码率、体积), 在其它维度都比完之后才轮到它们.

    这样「720p 带中文字幕」不会因为体积大就压过「1080p 没字幕」的档位差距, 而同档位时字幕
    仍能优先于码率与体积. 时长未知时码率取中性值 0, 落在有时长的候选之后 —— 拿不出时长就没
    法证明自己码率更高, 不拿体积冒充.
    """
    bitrate = entry.size / entry.duration if entry.duration is not None and entry.duration > 0 else 0.0
    return (-bitrate, -float(entry.size))


def _prefer_levels(prefer: Sequence[WashPrefer] | WashPrefer) -> tuple[WashPrefer, ...]:
    """配置里存的是有序列表; 老调用方传单个枚举或空列表时都退化成默认.

    同一个维度重复出现没有意义 (第二次比的值完全一样), 去掉重复保序.
    """
    if isinstance(prefer, WashPrefer):
        return (prefer,)
    ordered: list[WashPrefer] = []
    for item in prefer:
        if item not in ordered:
            ordered.append(item)
    return tuple(ordered) or (WashPrefer.QUALITY,)


def local_sort_key(entry: WashEntry, prefer: Sequence[WashPrefer] | WashPrefer = WashPrefer.QUALITY) -> tuple:
    """本地组的排序键 (升序即优先级从高到低).

    按配置里排定的维度顺序拼接; 画质的码率与体积两档固定排在所有维度之后 (见 ``_quality_tail``),
    因为它们只在档位相同时才有区分力. 最后两档固定是容器与路径, 只用来保证同样的输入每次选出
    同一份 —— 用户只选了「按体积」时, 体积相同才轮到它们.
    """
    levels = _prefer_levels(prefer)
    head: list[float] = []
    for level in levels:
        head.extend(_prefer_key(entry, level))
    tail = _quality_tail(entry) if WashPrefer.QUALITY in levels else ()
    return (*head, *tail, container_rank(entry.path), entry.path)


def strm_sort_key(entry: WashEntry, prefer: StrmPrefer = StrmPrefer.PATH) -> tuple:
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
    entry: WashEntry,
    prefer: Sequence[WashPrefer] | WashPrefer = WashPrefer.QUALITY,
    strm_prefer: StrmPrefer = StrmPrefer.PATH,
) -> tuple:
    """按条目的 kind 选排序键; 本地与 STRM 各有一套策略, 调用方须保证同一组 kind 一致.

    STRM 只让字幕维度参与: 指针的体积与档位都不可信, 但「云盘那份有没有中文字幕」是实情.
    """
    if entry.kind is WashRecordKind.STRM:
        if WashPrefer.SUBTITLE in _prefer_levels(prefer):
            return (float(entry.subtitle_rank), *strm_sort_key(entry, strm_prefer))
        return strm_sort_key(entry, strm_prefer)
    return local_sort_key(entry, prefer)


def build_groups(
    entries: Iterable[WashEntry],
    *,
    min_size_bytes: int,
    prefer: Sequence[WashPrefer] | WashPrefer = WashPrefer.QUALITY,
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
