# coding: utf-8
"""
epub2cbz.py - 文字ものの EPUB（小説など）を、ページ画像の CBZ に変換する

サーバーの EPUB 表示（PyMuPDF）は縦書きもルビも組めず、ルビが親文字の後ろに
ベタ書きされて読みにくい。PyMuPDF が入らない NAS 版ではそもそも本文が出せない。
そこで PC の Chrome / Edge をヘッドレスで動かして組版させ（縦書き・ルビ・縦中横・
傍点がそのまま効く）、PDF 経由でページ画像にして CBZ にまとめる。
出来た CBZ は普通の書庫として PC 版・NAS 版どちらのサーバーでも読める。

使い方:
    python scripts/epub2cbz.py <EPUB または フォルダ> [...]
    python scripts/epub2cbz.py "\\\\NAS\\comic\\小説" --font-mm 4.4

既定では EPUB と同じフォルダに「同名.cbz」を作る（既にあればスキップ。--force で作り直し）。
必要なもの: Chrome か Edge / pip install pymupdf pillow
"""
from __future__ import annotations

import argparse
import html
import io
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pymupdf
from PIL import Image, ImageChops

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
IMG_RE = re.compile(
    r'''<(?:img|image)\b[^>]*?\b(?:src|xlink:href|href)\s*=\s*["']([^"']+)["']''',
    re.IGNORECASE | re.DOTALL,
)

BROWSER_CANDIDATES = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
]

def find_browser() -> str:
    for c in BROWSER_CANDIDATES:
        p = os.path.expandvars(c)
        if os.path.isfile(p):
            return p
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    sys.exit("Chrome / Edge が見つかりません。--browser で実行ファイルを指定してください。")

def override_css(a: argparse.Namespace) -> str:
    """本の CSS の後ろに足す上書き。縦書き/横書きは本の指定（.vrtl/.hltr）に任せる。"""
    w, h = a.page_mm
    mv, mh = 7, 6   # 余白 mm（上下・左右）
    gothic = '"BIZ UDGothic", "Yu Gothic Medium", "Yu Gothic", sans-serif'
    # 本文中の画像がページからはみ出すと、Chrome は印刷時にページ全体を縮小して収めようとし、
    # その文書だけ文字が小さくなる。画像は必ず版面の内側に収める。
    return f"""
@page {{ size: {w}mm {h}mm; margin: {mv}mm {mh}mm; }}
img, svg {{ max-width: {w - mh * 2 - 1}mm !important; max-height: {h - mv * 2 - 1}mm !important; object-fit: contain; }}
html {{ font-size: {a.font_mm}mm !important; }}
html, body {{ margin: 0 !important; padding: 0 !important; background: #fff; color: #000; }}
body, p, div, span, h1, h2, h3, h4, h5, h6 {{ font-family: "{a.font}", "Yu Mincho", serif; }}
.gfont, .gfont * {{ font-family: {gothic} !important; }}
body {{ line-height: {a.line_height} !important; text-align: justify; line-break: strict; }}
rt {{ font-size: 0.5em; font-family: {gothic}; }}
.tcy {{ text-combine-upright: all; }}
.em-dot, .em-sesame {{ text-emphasis-position: over right; }}
a {{ color: inherit; text-decoration: none; }}
"""

def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1].lower()

def read_spine(root: Path) -> tuple[list[tuple[Path, str]], Path | None]:
    """展開済み EPUB の spine を (ファイル, media-type) の並びで返す。併せて表紙画像も返す。"""
    container = ElementTree.parse(root / "META-INF" / "container.xml").getroot()
    opf_rel = next(el.get("full-path") for el in container.iter()
                   if _local(el.tag) == "rootfile" and el.get("full-path"))
    opf_path = root / urllib.parse.unquote(opf_rel)
    opf = ElementTree.parse(opf_path).getroot()
    manifest: dict[str, tuple[Path, str]] = {}
    spine: list[str] = []
    cover_id = ""
    for el in opf.iter():
        tag = _local(el.tag)
        if tag == "item" and el.get("id") and el.get("href"):
            href = urllib.parse.unquote(el.get("href")).split("#", 1)[0]
            manifest[el.get("id")] = (opf_path.parent / href, (el.get("media-type") or "").lower())
            if "cover-image" in (el.get("properties") or "").split():
                cover_id = el.get("id")
        elif tag == "meta" and (el.get("name") or "").lower() == "cover" and not cover_id:
            cover_id = el.get("content") or ""
        elif tag == "itemref" and el.get("idref"):
            spine.append(el.get("idref"))
    docs = [manifest[i] for i in spine if i in manifest and manifest[i][0].is_file()]
    cover = manifest.get(cover_id, (None, ""))[0]
    return docs, cover if cover and cover.is_file() else None

def doc_images(doc_path: Path, text: str) -> list[Path]:
    out = []
    for m in IMG_RE.finditer(text):
        href = urllib.parse.unquote(html.unescape(m.group(1))).split("#", 1)[0]
        if not href or re.match(r"^[a-z][a-z0-9+.-]*:", href, re.IGNORECASE):
            continue
        p = Path(posixpath.normpath((doc_path.parent / href).as_posix()))
        if p.is_file() and p.suffix.lower() in IMAGE_EXT and p not in out:
            out.append(p)
    return out

def has_text(text: str) -> bool:
    body = re.sub(r"(?is)<head\b.*?</head>|<script\b.*?</script>|<style\b.*?</style>|<title\b.*?</title>", "", text)
    body = html.unescape(re.sub(r"<[^>]+>", "", body))
    return bool(re.sub(r"[\s　]+", "", body))

def is_blank(img: Image.Image) -> bool:
    lo, hi = img.convert("L").resize((64, 64)).getextrema()
    return hi - lo < 12

def print_pdf(browser: str, xhtml: Path, css: str, pdf: Path, profile: Path) -> None:
    text = xhtml.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"(?is)<script\b[^>]*?/>|<script\b.*?</script>", "", text)
    style = f'<style type="text/css">{css}</style>'
    if re.search(r"(?i)</head>", text):
        text = re.sub(r"(?i)</head>", lambda _: style + "</head>", text, count=1)
    else:
        text = re.sub(r"(?i)<body\b", lambda _: style + "<body", text, count=1)
    # 相対パス（CSS・画像）が切れないよう、元の XHTML と同じフォルダに置く
    patched = xhtml.with_name("__e2c_" + xhtml.name)
    patched.write_text(text, encoding="utf-8")
    try:
        r = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             "--disable-extensions", f"--user-data-dir={profile}",
             f"--print-to-pdf={pdf}", patched.as_uri()],
            capture_output=True, text=True, errors="ignore", timeout=600,
        )
    finally:
        patched.unlink(missing_ok=True)
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise RuntimeError(f"組版に失敗: {xhtml.name}\n{r.stderr[-1500:]}")

def page_image(page: pymupdf.Page, height_px: int) -> tuple[bytes, str] | None:
    """PDF の1ページを画像化して (バイト列, 拡張子) を返す。真っ白なページは None。

    文字だけのページは 16 階調グレーの PNG にする。JPEG より小さく（約1/2〜1/3）、
    文字の縁にノイズも出ない。絵が載っているページだけ JPEG にする。
    """
    zoom = height_px / page.rect.height
    colored = bool(page.get_images(full=True))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                          colorspace=pymupdf.csRGB if colored else pymupdf.csGRAY, alpha=False)
    img = Image.frombytes("RGB" if colored else "L", (pix.width, pix.height), pix.samples)
    if is_blank(img):
        return None
    if colored:
        r, g, b = img.split()
        if ImageChops.difference(r, g).getbbox() or ImageChops.difference(g, b).getbbox():
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88)
            return buf.getvalue(), ".jpg"
        img = g   # 外字など白黒の絵しか無いページは文字ページと同じ扱い
    img = img.point(lambda v: (v + 8) // 17 * 17).quantize(16)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True, bits=4)
    return buf.getvalue(), ".png"

def convert(epub: Path, out: Path, a: argparse.Namespace) -> int:
    t0 = time.time()
    css = override_css(a)
    pages: list[tuple[bytes, str]] = []
    with tempfile.TemporaryDirectory(prefix="e2c_") as tmp:
        root, profile = Path(tmp) / "book", Path(tmp) / "profile"
        with zipfile.ZipFile(epub) as zf:
            zf.extractall(root)
        docs, cover = read_spine(root)
        used: set[Path] = set()

        def add_image(p: Path) -> None:
            if p in used:
                return
            used.add(p)
            try:
                with Image.open(p) as im:
                    if is_blank(im):
                        return   # 余白合わせ用の白紙画像
            except Exception:
                return
            pages.append((p.read_bytes(), p.suffix.lower()))

        for n, (doc, mtype) in enumerate(docs, 1):
            if mtype.startswith("image/"):
                add_image(doc)
                continue
            text = doc.read_text(encoding="utf-8", errors="ignore")
            if not has_text(text):
                # 表紙・口絵・挿絵だけのページ。組版に通さず原本の画像をそのまま使う
                for img in doc_images(doc, text):
                    add_image(img)
                continue
            pdf = Path(tmp) / f"{n:04d}.pdf"
            print_pdf(a.browser, doc, css, pdf, profile)
            with pymupdf.open(pdf) as d:
                for page in d:
                    got = page_image(page, a.height_px)
                    if got:
                        pages.append(got)
            print(f"\r  組版中 {n}/{len(docs)}  {len(pages)}ページ", end="", flush=True)
        print()

        if cover and cover not in used and pages:
            pages.insert(0, (cover.read_bytes(), cover.suffix.lower()))
    if not pages:
        raise RuntimeError("ページが1枚も作れませんでした")

    tmp_out = out.with_name(out.name + ".part")
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_STORED) as zf:
        for i, (data, ext) in enumerate(pages, 1):
            zf.writestr(f"{i:05d}{ext}", data)
    os.replace(tmp_out, out)
    mb = out.stat().st_size / 1e6
    print(f"  → {out.name}  {len(pages)}ページ / {mb:.0f}MB / {time.time() - t0:.0f}秒")
    return len(pages)

def _page_mm(s: str) -> tuple[float, float]:
    w, h = re.split(r"[x×,]", s.lower())
    return float(w), float(h)

def main() -> None:
    ap = argparse.ArgumentParser(description="文字ものの EPUB をページ画像の CBZ に変換する（縦書き・ルビ対応）")
    ap.add_argument("inputs", nargs="+", type=Path, help="EPUB ファイル、または EPUB の入ったフォルダ（下の階層も探す）")
    ap.add_argument("--out", type=Path, help="出力フォルダ（省略時は EPUB と同じ場所）")
    ap.add_argument("--font-mm", type=float, default=4.0, help="本文の文字サイズ mm（既定 4.0。大きくするとページ数が増える）")
    ap.add_argument("--page-mm", type=_page_mm, default=(100.0, 160.0), help="1ページの大きさ 幅x高さ mm（既定 100x160）")
    ap.add_argument("--line-height", type=float, default=1.85, help="行送り（既定 1.85。ルビが入る余白を含む）")
    ap.add_argument("--font", default="BIZ UDMincho Medium", help="本文フォント（既定 BIZ UDMincho Medium）")
    ap.add_argument("--height-px", type=int, default=1800, help="ページ画像の高さ px（既定 1800）")
    ap.add_argument("--browser", help="Chrome / Edge の実行ファイル（省略時は自動で探す）")
    ap.add_argument("--force", action="store_true", help="CBZ が既にあっても作り直す")
    a = ap.parse_args()
    a.browser = a.browser or find_browser()
    # コンソールが cp932 だと「〜」などを含むファイル名の表示で落ちるので、出せない文字は ? にする
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    epubs: list[Path] = []
    for p in a.inputs:
        if p.is_dir():
            epubs += sorted(p.rglob("*.epub"))
        elif p.suffix.lower() == ".epub" and p.is_file():
            epubs.append(p)
        else:
            print(f"スキップ（EPUB ではありません）: {p}")
    if not epubs:
        sys.exit("EPUB が見つかりません。")

    failed = 0
    for epub in epubs:
        out_dir = a.out or epub.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / (epub.stem + ".cbz")
        print(epub.name)
        if out.exists() and not a.force:
            print("  CBZ が既にあるのでスキップ（作り直すなら --force）")
            continue
        try:
            convert(epub, out, a)
        except Exception as e:
            failed += 1
            print(f"  [失敗] {e}")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
