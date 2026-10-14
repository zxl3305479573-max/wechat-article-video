"""Shared constants for article-video generation stages."""

from typing import Any

FOOTER_CAPTIONS = (
    "买优质塑化原料",
    "就找兆企供应链",
    "兆企供应链管理有限公司深耕塑化行业",
    "货源稳定 物流高效",
    "找兆企采购{product_grade}",
    "让生产更顺畅",
    "本视频禁止用于ai模型训练",
    "兆企供应链拥有最终解释权",
)

# Part of every logo-marked image's cache hash. Both the local-asset stage
# and the draft adapter must agree on it, so it is defined only here.
LOGO_RENDER_VERSION = b"direct-source-logo-v7"


def shot_image_paths(shot: dict[str, Any]) -> list[str]:
    """Return a shot's image paths, accepting the current and legacy schema.

    Shots now carry parallel `image_paths`; older jobs stored one `image_path`.
    Every reader must resolve both the same way, so this is the only place that
    decides which form wins.
    """
    paths = shot.get("image_paths")
    if isinstance(paths, list):
        values = [path for path in paths if isinstance(path, str) and path.strip()]
        if values:
            return values
    legacy = shot.get("image_path")
    return [legacy] if isinstance(legacy, str) and legacy.strip() else []
