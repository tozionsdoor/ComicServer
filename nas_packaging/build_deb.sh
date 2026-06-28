#!/bin/bash
# build_deb.sh - ArcHive NAS 用 .deb ビルドスクリプト
# Linux (WSL / NAS 上 SSH) で実行してください。
# 使い方: bash build_deb.sh
# 出力:   archiveserver_1.0.0_armel.deb

set -e

PKG="archiveserver"
VER="1.0.0"
ARCH="armel"
DEB_NAME="${PKG}_${VER}_${ARCH}.deb"
BUILD_DIR="$(pwd)/deb_build"

echo "=== ArcHive .deb ビルド開始 ==="

# ── ビルドディレクトリ作成 ──────────────────────────────────────────────
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/DEBIAN"
mkdir -p "$BUILD_DIR/apps/archiveserver/server"
mkdir -p "$BUILD_DIR/usr/share/doc/archiveserver"

# ── DEBIAN メタデータ ────────────────────────────────────────────────────
cp DEBIAN/control  "$BUILD_DIR/DEBIAN/control"
cp DEBIAN/postinst "$BUILD_DIR/DEBIAN/postinst"
cp DEBIAN/prerm    "$BUILD_DIR/DEBIAN/prerm"
chmod 755 "$BUILD_DIR/DEBIAN/postinst"
chmod 755 "$BUILD_DIR/DEBIAN/prerm"

# ── アプリ本体 ───────────────────────────────────────────────────────────
cp manga_server_nas.py            "$BUILD_DIR/apps/archiveserver/server/"
cp config.xml                     "$BUILD_DIR/apps/archiveserver/"
cp fvapp-archiveserver.service    "$BUILD_DIR/apps/archiveserver/"
cp archive_environment            "$BUILD_DIR/apps/archiveserver/"

# ロゴ（存在すれば）
if [ -f logo.png ]; then
    cp logo.png "$BUILD_DIR/apps/archiveserver/"
fi

# ── systemd サービスファイルを /lib/systemd/system にも置く ───────────────
mkdir -p "$BUILD_DIR/lib/systemd/system"
cp fvapp-archiveserver.service "$BUILD_DIR/lib/systemd/system/"

# ── ドキュメント ─────────────────────────────────────────────────────────
cat > "$BUILD_DIR/usr/share/doc/archiveserver/copyright" << 'EOF'
ArcHive Server
Copyright 2025 ArcHive
https://tozionsdoor.github.io/ComicServer/

This software is distributed under the MIT License.
EOF

# ── .deb ビルド ──────────────────────────────────────────────────────────
dpkg-deb --build --root-owner-group "$BUILD_DIR" "$DEB_NAME"

echo ""
echo "=== ビルド完了 ==="
echo "出力ファイル: $DEB_NAME"
echo ""
echo "NAS へのインストール方法:"
echo "  1. ファイルを NAS にコピー:  scp $DEB_NAME admin@NAS_IP:/tmp/"
echo "  2. NAS に SSH でログイン:    ssh admin@NAS_IP"
echo "  3. インストール:             dpkg -i /tmp/$DEB_NAME"
echo "  4. 管理ページを開く:         http://NAS_IP:8765/admin"
echo ""
echo "スキャンフォルダの設定:"
echo "  /apps/archiveserver/manga_server_config.json を編集し、"
echo "  scan_dirs に NAS 上の書庫フォルダのパスを追加してください。"
echo "  例: \"scan_dirs\": [\"/data/comics\", \"/data/books\"]"
echo "  設定後: systemctl restart fvapp-archiveserver"

rm -rf "$BUILD_DIR"
