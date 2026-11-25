from __future__ import annotations

import io
import zipfile
from pathlib import Path

from .models import SECRETS_HOT_FILENAME
from .recorder import task_dir_for


def build_record_zip(log_dir: Path, task_id: int) -> bytes:
    """打包任务记录, 一律排除 ``.secrets.hot.json``.

    那份旁路文件含站点 cookie 与大模型密钥, 只在宿主机上留作调试; 导出走 HTTP, 不给明文通道.
    """
    root = task_dir_for(log_dir, task_id)
    if not root.is_dir() or not (root / "manifest.json").is_file():
        raise FileNotFoundError(f"record not found for task {task_id}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            # 旁路密文不进入 zip
            if path.name == SECRETS_HOT_FILENAME:
                continue
            zf.write(path, arcname=str(Path(f"task-{task_id}") / rel))
    return buf.getvalue()
