"""通知渠道: 把一段标题与正文推到使用者配置的地址."""

from .channels import NotifyRequest, build_request
from .service import Notifier, NotifyOutcome

__all__ = ["Notifier", "NotifyOutcome", "NotifyRequest", "build_request"]
