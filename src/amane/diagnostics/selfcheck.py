from pathlib import Path

from pydantic import BaseModel

from ..config.manager import HotSettings


class CheckItem(BaseModel):
    key: str
    ok: bool
    detail: str = ""


class SelfCheckReport(BaseModel):
    items: list[CheckItem] = []

    @property
    def failed(self) -> list[CheckItem]:
        return [item for item in self.items if not item.ok]


class SelfCheck:
    """只读体检: 每项探测不写库、不改文件, 因此随时可在请求里跑.

    深度自检与模拟运行耗时超过事件循环预算, 走 ``DIAGNOSE`` 任务, 不在请求线程里展开.
    """

    def __init__(self, hot: HotSettings, data_dir: Path) -> None:
        self._hot = hot
        self._data_dir = data_dir

    async def run(self) -> SelfCheckReport:
        # TODO(phase2): 见 amane-merge/notes/diagnostics.md
        return SelfCheckReport()
