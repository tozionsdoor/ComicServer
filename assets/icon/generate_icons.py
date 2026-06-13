"""ArcHive アイコン一括生成スクリプト

assets/icon/AH_rogo.png (黒背景＋オレンジ「AH」モノグラム＋白いアーチの六角形シルエット) を
全サイズのアイコンの元画像として使用する。

Android アプリアイコン(legacy + adaptive)、サーバー用 .ico、
docs用 favicon、サーバーのシステムトレイ用アイコンを生成する。

使い方: python assets/icon/generate_icons.py
"""
import io
import struct
from pathlib import Path
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent.parent  # ComicServer/
ICON_DIR = Path(__file__).resolve().parent            # assets/icon/
SRC_LARGE = ICON_DIR / "AH_rogo4.png"
SRC_SMALL = ICON_DIR / "Book.png"  # 16px専用（縮小しても潰れにくいデザイン）

BG_COLOR = (0, 0, 0)
ANDROID_RES = ROOT / "comicserver_app" / "android" / "app" / "src" / "main" / "res"

LEGACY_SIZES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}
FOREGROUND_SIZES = {
    "mdpi": 108,
    "hdpi": 162,
    "xhdpi": 216,
    "xxhdpi": 324,
    "xxxhdpi": 432,
}


def make_large_icon(size: int) -> Image.Image:
    """AH_rogo4.png (六角形＋AH＋本) をLanczos縮小して使う。16pxはBook.pngを使う。"""
    src_path = SRC_SMALL if size <= 16 else SRC_LARGE
    src = Image.open(src_path).convert("RGB")
    return src.resize((size, size), Image.LANCZOS).convert("RGBA")



def make_foreground_master(canvas_size: int = 1024, safe_fraction: float = 0.62) -> Image.Image:
    """黒背景を透過にし、セーフゾーンに収まるよう中央配置したRGBA画像を作る。"""
    src_rgb = Image.open(SRC_LARGE).convert("RGB")
    diff = Image.new("RGB", src_rgb.size, BG_COLOR)
    d = ImageChops.difference(src_rgb, diff).convert("L")
    bbox = d.point(lambda p: 255 if p > 8 else 0).getbbox()
    cropped_rgb = src_rgb.crop(bbox)
    cropped_diff = d.crop(bbox)

    alpha = cropped_diff.point(lambda p: min(255, p * 4))
    cropped_rgba = cropped_rgb.convert("RGBA")
    cropped_rgba.putalpha(alpha)

    cw, ch = cropped_rgba.size
    target = int(canvas_size * safe_fraction)
    if cw >= ch:
        new_w, new_h = target, round(ch * target / cw)
    else:
        new_h, new_w = target, round(cw * target / ch)
    resized = cropped_rgba.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    x = (canvas_size - new_w) // 2
    y = (canvas_size - new_h) // 2
    canvas.paste(resized, (x, y), resized)
    return canvas


def save_multisize_ico(path, images):
    """images: [(size, PIL.Image), ...] をサイズ別のPNGエントリとして .ico に手書きで保存する。

    Pillow の標準 ICO 保存は1枚を各サイズへ縮小するだけなので、
    サイズごとに異なる元画像を埋め込めるよう自前で構築する。
    """
    blobs = []
    for s, img in images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        blobs.append((s, buf.getvalue()))

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, data = b"", b""
    for s, blob in blobs:
        dim = 0 if s >= 256 else s  # 256 は 0 として記録
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob
    Path(path).write_bytes(header + entries + data)


def main():
    # --- Android legacy icons ---
    for density, size in LEGACY_SIZES.items():
        out = make_large_icon(size)
        d = ANDROID_RES / f"mipmap-{density}"
        d.mkdir(parents=True, exist_ok=True)
        out.save(d / "ic_launcher.png")
        print("wrote", d / "ic_launcher.png", out.size)

    # --- Android adaptive icon foreground ---
    fg_master = make_foreground_master()
    fg_master.save(ICON_DIR / "app_icon_foreground.png")
    for density, size in FOREGROUND_SIZES.items():
        out = fg_master.resize((size, size), Image.LANCZOS)
        d = ANDROID_RES / f"mipmap-{density}"
        d.mkdir(parents=True, exist_ok=True)
        out.save(d / "ic_launcher_foreground.png")
        print("wrote", d / "ic_launcher_foreground.png", out.size)

    # --- adaptive icon XML + background color ---
    values_dir = ANDROID_RES / "values"
    values_dir.mkdir(parents=True, exist_ok=True)
    colors_xml = values_dir / "colors.xml"
    bg_hex = "#%02X%02X%02X" % BG_COLOR
    colors_xml.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<resources>\n'
        f'    <color name="ic_launcher_background">{bg_hex}</color>\n'
        '</resources>\n',
        encoding="utf-8",
    )
    print("wrote", colors_xml)

    anydpi_dir = ANDROID_RES / "mipmap-anydpi-v26"
    anydpi_dir.mkdir(parents=True, exist_ok=True)
    (anydpi_dir / "ic_launcher.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <background android:drawable="@color/ic_launcher_background"/>\n'
        '    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>\n'
        '</adaptive-icon>\n',
        encoding="utf-8",
    )
    print("wrote", anydpi_dir / "ic_launcher.xml")

    # --- Server .ico (全サイズ AH_rogo.png で統一) ---
    ico_path = ICON_DIR / "app_icon.ico"
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    ico_images = [(s, make_large_icon(s)) for s in ico_sizes]
    save_multisize_ico(ico_path, ico_images)
    print("wrote", ico_path)

    # --- サーバー システムトレイ用アイコン (Book.png=16pxアイコンと同じデザイン) ---
    tray_icon = Image.open(SRC_SMALL).convert("RGB").resize((256, 256), Image.LANCZOS).convert("RGBA")
    tray_icon_path = ICON_DIR / "tray_icon.png"
    tray_icon.save(tray_icon_path)
    print("wrote", tray_icon_path, tray_icon.size)

    # --- docs favicon (全サイズ AH_rogo.png で統一) ---
    docs_images = ROOT / "docs" / "images"
    docs_images.mkdir(parents=True, exist_ok=True)
    favicon_path = docs_images / "favicon.ico"
    favicon_sizes = [16, 32, 48]
    favicon_images = [(s, make_large_icon(s)) for s in favicon_sizes]
    save_multisize_ico(favicon_path, favicon_images)
    print("wrote", favicon_path)

    apple_touch = make_large_icon(180)
    apple_touch_path = docs_images / "apple-touch-icon.png"
    apple_touch.save(apple_touch_path)
    print("wrote", apple_touch_path)


if __name__ == "__main__":
    main()
