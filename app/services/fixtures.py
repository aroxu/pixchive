"""Explicit, synthetic local fixtures. Never run automatically at application startup."""

import io
import math

from PIL import Image, ImageDraw

from app.adapters.pixiv.contracts import RemoteArtwork, RemoteTag
from app.core.config import Settings
from app.services import library, storage

TITLES = [
    "조용한 궤도",
    "夕暮れの庭",
    "파도가 머무는 곳",
    "빛의 조각",
    "푸른 여름의 기록",
    "夜の窓",
]
PALETTES = [
    ("#203a3d", "#7ac5b6", "#dae9cb"),
    ("#412f3d", "#bd8879", "#eed1a2"),
    ("#1c3449", "#548b9d", "#acc8cc"),
    ("#3d3a32", "#c2a779", "#e9d9b1"),
    ("#223d34", "#7d9e7d", "#d4dca6"),
    ("#272d47", "#777ba4", "#c9c7e5"),
]


def image_bytes(index: int, page: int = 0) -> bytes:
    width, height = (720, 920) if index % 3 != 1 else (900, 680)
    background, middle, light = PALETTES[index % len(PALETTES)]
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    radius = 120 + index * 8
    x, y = width * 0.6, height * 0.3
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=light)
    for layer in range(3):
        points = [(0, height), (width, height)]
        points += [
            (px, height * (0.65 + layer * 0.09) + math.sin(px / 120 + layer + index + page) * 60)
            for px in range(width, -1, -4)
        ]
        draw.polygon(points, fill=[middle, background, middle][layer])
    draw.rectangle((35, 35, width - 35, height - 35), outline=light, width=1)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def seed(settings: Settings):
    for i, title in enumerate(TITLES):
        remote = RemoteArtwork(
            id=str(900000000 + i),
            artist_id=str(800000000 + i % 3),
            artist_name=["아카이브 스튜디오", "月のアトリエ", "한여름"][i % 3],
            title=title,
            caption=(
                "로컬 개발용으로 생성한 샘플 작품입니다. 실제 Pixiv에서 수집한 자료가 아닙니다."
            ),
            kind="manga" if i == 1 else "illustration",
            rating="safe",
            page_count=3 if i == 1 else 1,
            work_tags=[
                RemoteTag(name="風景", translations={"en": "landscape"}),
                RemoteTag(name=["夜", "空", "海"][i % 3]),
            ],
            bookmark_tags=["분위기 참고"],
        )
        artwork = library.ingest(remote, "public" if i % 2 else "private", fixture=True)
        if artwork:
            for page in range(remote.page_count):
                storage.store_image(settings, artwork.id, page, image_bytes(i, page))
            library.edit_tags([artwork.id], "다시 보고 싶은", "add")
