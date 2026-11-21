from .base import RepositoryMixinBase


class WashRepoMixin(RepositoryMixinBase):
    """``wash_records`` 的读写面; 方法由 wash 分支在同一文件内补全.

    与 ``ops_transfer`` 同样的分工: 组合点已在 Phase 0 定稿, 分支只加方法.
    """
