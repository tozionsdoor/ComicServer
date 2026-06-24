# Play Console「アプリへのアクセス権」審査メモ（日英・コピペ用）

ArcHive は「自宅PCでサーバーを起動し、同じWi-Fiで初回ペアリングする」アプリのため、
Google の審査員はそのままではログイン画面から先に進めない。
そこで **審査用デモサーバー** を用意し、URL＋トークンを手入力して接続してもらう。

---

## 0. Play Console での設定場所

「アプリの内容」→「**アプリへのアクセス権 (App access)**」で、
**「すべての機能を利用するにはログイン等の特別なアクセスが必要」** を選び、
インストラクションを1件追加する。フォームの各欄には以下を入れる。

| Play Console の欄 | 入れる値 |
|---|---|
| 名前 / フローの説明 | `デモサーバーへの接続 / Connect to demo server` |
| ユーザー名 (Username) | デモサーバーURL（下記 `{{DEMO_URL}}`） |
| パスワード (Password) | デモ用トークン（下記 `{{DEMO_TOKEN}}`） |
| その他の説明 (Any other instructions) | 下の「審査メモ本文」を貼る |

> ※ ユーザー名/パスワード欄に収まらない場合や分かりにくい場合は、
> URLもトークンも「その他の説明」欄にまとめて記載してOK。

---

## 1. 審査メモ本文（日本語）

```
【このアプリについて】
ArcHive は、ユーザー自身の自宅PCに無料のサーバーソフトをインストールし、
スマホアプリからそのPCに接続して、自炊した本やPDFを閲覧するビューワーです。
開発者のサーバーに本をアップロードする仕組みではありません。
通常の利用では、初回のみ自宅のWi-Fi内でPCとペアリングする必要があります。

【審査用デモサーバーのご案内】
審査員の方が実機サーバーを用意しなくても全機能を確認できるよう、
サンプル書籍だけを入れたデモサーバーを用意しました。
以下の手順で接続してください（同じWi-Fiである必要はありません）。

1. アプリを起動するとログイン画面が表示されます。
2. 「サーバーURL」欄に次を入力してください：
   {{DEMO_URL}}
3. 「認証トークン」欄に次を入力してください：
   {{DEMO_TOKEN}}
   ※ 入力ミスを防ぐため、コピー＆ペーストを推奨します。
4. 「接続」ボタンを押してください。本棚画面に進みます。

※ 画面の「LAN内のサーバーを探す」は使わないでください
   （同じWi-Fi内のサーバーを探す機能で、デモ接続には不要です）。

【接続後にできること】
・本棚の一覧表示、本を開く、ページめくり
・見開き／単ページ表示の切り替え、右綴じ／左綴じの切り替え
・画面中央タップでメニュー表示、長押しで拡大ルーペ
・前の巻／次の巻への移動、読書履歴の記録

【ご不明な点】
接続できない場合や追加情報が必要な場合は、
to.zionsdoor@gmail.com までご連絡ください。デモサーバーは審査期間中、起動したままにします。
```

---

## 2. 審査メモ本文（英語）

```
[About this app]
ArcHive is a viewer app for reading your own scanned books and PDFs.
The user installs a free server program on their own home PC, and the mobile
app connects directly to that PC. Books are NOT uploaded to any developer
server. In normal use, the first-time pairing must be done on the same home
Wi-Fi as the PC.

[Demo server for review]
So that reviewers can verify all features without setting up their own server,
we have prepared a demo server that contains only sample books.
Please connect using the steps below (you do NOT need to be on any specific Wi-Fi):

1. Launch the app to see the login screen.
2. In the "サーバーURL" (Server URL) field, enter:
   {{DEMO_URL}}
3. In the "認証トークン" (Auth token) field, enter:
   {{DEMO_TOKEN}}
   (Copy & paste is recommended to avoid typos.)
4. Tap the "接続" (Connect) button. You will be taken to the bookshelf screen.

Note: Please do NOT use "LAN内のサーバーを探す" (Search for servers on LAN).
That feature looks for a server on the same Wi-Fi and is not needed for the demo.

[What you can try after connecting]
- Browse the bookshelf, open a book, turn pages
- Toggle two-page spread / single-page view, right-to-left / left-to-right binding
- Tap the center of the screen for the menu; long-press for a magnifier
- Move to previous / next volume; reading history is recorded

[Questions]
If you cannot connect or need more information, please contact
to.zionsdoor@gmail.com. The demo server will be kept running during the review period.
```

---

## 3. 確定前のTODO（URL・トークンが決まったら差し替え）

- [ ] cloudflared named tunnel を立て、固定HTTPSホスト名を取得 → `{{DEMO_URL}}` に反映
- [ ] `manga_server_config.json` の `devices` に審査専用エントリ追加（status=approved）→ `{{DEMO_TOKEN}}` に反映
- [ ] デモサーバーはサンプル本のみのフォルダを指すよう設定（個人の本棚・市販書籍を含めない）
- [ ] 審査期間中はデモサーバー＋トンネルを起動したままにする
- [ ] 公開後の更新でも再審査が走るため、当面はデモ環境を残す
