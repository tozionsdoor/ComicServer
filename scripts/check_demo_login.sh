#!/bin/sh
# Play Store審査用デモサーバーのログイン確認。
# review_notes.md からURL/トークンを毎回読み直すため、URLやトークンが
# 再発行されても本スクリプト自体・呼び出しコマンドは常に同一文字列のまま。
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
NOTES="$DIR/play_store/review_notes.md"

if [ ! -f "$NOTES" ]; then
  echo "FAIL:NOTFOUND:review_notes.mdが見つかりません($NOTES)"
  exit 0
fi

URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$NOTES" | head -1)
TOKEN=$(grep -oE '^[[:space:]]*[A-Za-z0-9_-]{25,}[[:space:]]*$' "$NOTES" | head -1 | tr -d '[:space:]')

if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "FAIL:NOTFOUND:review_notes.mdからURLまたはトークンを抽出できませんでした"
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
