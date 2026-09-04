"""キャッシュを故意に消して「巻頭ジャンプ」を再現し、サーバーの応答時間を測る。

アプリ実機では「キャッシュが切れた大きな本」を用意すること自体が難しく、
遅さの再現・改善確認ができない。このスクリプトは対象の本のページキャッシュを
削除してから、巻頭ジャンプ相当のバースト（本体10枚＋サムネ10枚を同時）を
実サーバーへ投げて所要時間を出す。

使い方:
    python scripts/bench_cold_page.py                 # 本を一覧して終了
    python scripts/bench_cold_page.py <bid>           # その本で測定
    python scripts/bench_cold_page.py <bid> --keep    # キャッシュを消さずに測定

bid は書庫内パスから md5(str(path))[:12] で決まる値で、引数なし実行の一覧に出る。
トークンは manga_server_config.json の devices から自動で拾う。
"""
import argparse
import concurrent.futures as cf
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
CACHE   = Path.home() / ".manga_server" / "cache" / "pages"
CTX     = ssl._create_unverified_context()


def load_token_and_port() -> tuple[str, int]:
    cfg = json.loads((APP_DIR / "manga_server_config.json").read_text(encoding="utf-8"))
    for dev in cfg.get("devices", {}).values():
        if dev.get("status") == "approved" and dev.get("token"):
            return dev["token"], int(cfg.get("port", 8765))
    sys.exit("承認済み端末のトークンが config に見つかりません")


def get(base: str, token: str, path: str, timeout: int = 300):
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return time.time() - t0, "OK", len(r.read())
    except Exception as e:
        return time.time() - t0, f"{type(e).__name__}:{str(e)[:40]}", 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bid", nargs="?", help="測定する本のID（省略で一覧表示）")
    ap.add_argument("--keep", action="store_true", help="キャッシュを削除せずに測る")
    ap.add_argument("--host", default="127.0.0.1", help="接続先ホスト（既定: 127.0.0.1）")
    ap.add_argument("--pages", type=int, default=10, help="同時に取るページ数（既定: 10）")
    args = ap.parse_args()

    token, port = load_token_and_port()
    base = f"https://{args.host}:{port}"

    if not args.bid:
        req = urllib.request.Request(base + "/api/books",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                books = json.loads(r.read())
        except Exception as e:
            sys.exit(f"サーバーに繋がりません: {type(e).__name__}: {e}")
        print(f"{len(books)} 冊。キャッシュ済みページ数の多い順に20冊:")
        counts: dict[str, int] = {}
        for f in CACHE.glob("*_1800.jpg"):
            counts[f.name.split("_")[0]] = counts.get(f.name.split("_")[0], 0) + 1
        titles = {b["id"]: b["title"] for b in books}
        for bid, c in sorted(counts.items(), key=lambda kv: -kv[1])[:20]:
            print(f"  {bid}  {c:5d}枚  {titles.get(bid, '(不明)')[:56]}")
        return

    bid = args.bid
    if not args.keep:
        removed = 0
        for f in CACHE.glob(f"{bid}_*.jpg"):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        print(f"キャッシュ削除: {removed} ファイル")

    dt, st, _ = get(base, token, f"/api/books/{bid}/info", timeout=60)
    print(f"/info : {dt:6.2f}s {st}")
    if st != "OK":
        return

    n = args.pages
    reqs = [(f"本体 p{i}", f"/api/books/{bid}/pages/{i}") for i in range(n)]
    reqs += [(f"サムネ p{i}", f"/api/books/{bid}/pages/{i}?w=160") for i in range(n)]

    print(f"\n同時 {len(reqs)} リクエスト発射...")
    t_all = time.time()
    with cf.ThreadPoolExecutor(max_workers=len(reqs)) as ex:
        futs = {ex.submit(get, base, token, url): label for label, url in reqs}
        results = []
        for fu in cf.as_completed(futs):
            d, s, sz = fu.result()
            results.append((d, futs[fu], s, sz))
    total = time.time() - t_all

    results.sort()
    for d, label, s, sz in results:
        print(f"  {label:10s} {d:8.2f}s  {s:26s} {sz/1024:7.0f} KB")
    ng = sum(1 for r in results if r[2] != "OK")
    print(f"\n全体 {total:.2f}s / 最遅 {max(r[0] for r in results):.2f}s / 失敗 {ng} 件")


if __name__ == "__main__":
    main()
