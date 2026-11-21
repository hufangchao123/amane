from .base import RepositoryMixinBase


class TransferRepoMixin(RepositoryMixinBase):
    """``transfer_items`` 的读写面; 方法由 transfer 分支在同一文件内补全.

    Phase 0 只挂这个类, 让 ``Repository`` 组合与导出清单一次定稿: 三条并行分支各自往自己的
    mixin 文件里加方法, 不再触碰 ``repository.py`` / ``repos/__init__.py``, 合并时零冲突.
    """
