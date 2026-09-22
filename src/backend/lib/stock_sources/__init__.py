"""

Replaces the AGPLv3-licensed ``vendor/OpenMontage/tools/video/stock_sources``.
Each adapter is implemented from scratch against the provider's public API
documentation — same conceptual interface, zero copied code.
"""
from .base import Candidate, SearchFilters, StockSource
from .pexels import PexelsSource
from .pixabay import PixabaySource
from .coverr import CoverrSource

__all__ = [
    "Candidate", "SearchFilters", "StockSource",
    "PexelsSource", "PixabaySource", "CoverrSource",
]
