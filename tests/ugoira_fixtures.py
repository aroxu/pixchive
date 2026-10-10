import io
import zipfile

from PIL import Image

from app.adapters.pixiv.contracts import RemoteArtwork, UgoiraFrame


def animation(artwork_id="124"):
    return RemoteArtwork(
        id=artwork_id,
        artist_id="42",
        artist_name="합성 작가",
        title="합성 애니메이션",
        kind="ugoira",
        rating="safe",
        page_count=1,
        ugoira_zip_url="https://i.pximg.net/synthetic_ugoira1920x1080.zip",
        ugoira_frames=[
            UgoiraFrame(file="000000.png", delay=120),
            UgoiraFrame(file="000001.png", delay=280),
        ],
    )


def zip_bytes(names=("000000.png", "000001.png")):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, name in enumerate(names):
            image = Image.new("RGB", (64, 48), (30 + index * 150, 130, 160))
            data = io.BytesIO()
            image.save(data, "PNG")
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data.getvalue())
    return output.getvalue()
