"""115 中转: 网盘客户端、广告判定与状态聚合. 搬移编排在 ``handlers/transfer115.py``."""

from .client import (
    CloudEntry,
    Transfer115Client,
    TransferClientLike,
    TransferError,
    TransferNotConfigured,
)
from .rules import has_number, is_ad_file, is_media_name, pick_folder_ads
from .service import TransferService, TransferSummary

__all__ = [
    "CloudEntry",
    "Transfer115Client",
    "TransferClientLike",
    "TransferError",
    "TransferNotConfigured",
    "TransferService",
    "TransferSummary",
    "has_number",
    "is_ad_file",
    "is_media_name",
    "pick_folder_ads",
]
