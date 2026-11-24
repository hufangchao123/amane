"""上传体积上限的读取.

先整块 ``await file.read()`` 再判大小, 上限形同虚设: 客户端可以先把几百 MB 读进内存。
这里按块读, 一旦累计超限立即停止并拒绝。
"""

from fastapi import HTTPException, UploadFile

CHUNK_SIZE = 1024 * 1024
"""每次读取的字节数."""


async def read_limited(
    file: UploadFile,
    *,
    limit: int,
    too_large: str,
    empty: str | None = None,
) -> bytes:
    """按 ``limit`` 上限读入 ``file``.

    Args:
        file: 上传的文件.
        limit: 允许的最大字节数.
        too_large: 超限时的错误文案.
        empty: 空文件的错误文案; 传 ``None`` 允许空文件.

    Raises:
        HTTPException: 超限返回 413, 空文件返回 422.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(CHUNK_SIZE):
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=too_large)
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload and empty is not None:
        raise HTTPException(status_code=422, detail=empty)
    return payload


__all__ = ["CHUNK_SIZE", "read_limited"]
