#!/bin/sh
# Play Store審査用デモサーバーのログイン確認。
# URLは review_notes.md、トークンは git管理外の review_secrets.local.md から毎回読み直す
# （公開リポジトリにトークンを置かないため）。URLやトークンが再発行されても、
# 本スクリプト自体・呼び出しコマンドは常に同一文字列のまま。
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
NOTES="$DIR/play_store/review_notes.md"
SECRETS="$DIR/play_store/review_secrets.local.md"

if [ ! -f "$NOTES" ]; then
  echo "FAIL:NOTFOUND:review_notes.mdが見つかりません($NOTES)"
  exit 0
fi

URL=$(grep -oE 'https://([a-zA-Z0-9.-]+\.trycloudflare\.com|archive-demo\.uk)' "$NOTES" | head -1)
TOKEN=$(grep -E '^DEMO_TOKEN=' "$SECRETS" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')

if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "FAIL:NOTFOUND:URL(review_notes.md)またはトークン(review_secrets.local.md)を抽出できませんでした"
  exit 0
fi

BODY_FILE="$(mktemp)"
HTTP_CODE=$(curl -s -o "$BODY_FILE" -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "$URL/api/status" --max-time 15 || echo "000")
BODY=$(cat "$BODY_FILE" 2>/dev/null | head -c 300)
rm -f "$BODY_FILE"

if [ "$HTTP_CODE" = "200" ] && echo "$BODY" | grep -q '"books"'; then
  echo "OK:$URL"
else
  echo "FAIL:$HTTP_CODE:$URL:$BODY"
fi
