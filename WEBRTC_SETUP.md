# WebRTC P2P セットアップ手順

Phase 2 の WebRTC P2P（外出先からVPN無しで自宅サーバーに繋ぐ機能）を有効化する手順。
**この作業は開発者が一度だけ行えばよい。** エンドユーザーはFirebase作業不要
（アプリ・サーバーに設定が同梱される）。

設計の全体像は memory `07_webrtc_design.md` 参照。

---

## 仕組み（要約）

```
スマホアプリ ──(LAN/IPv6 HTTP直結が最優先)──→ 自宅PCサーバー   ← 家ではこれ（最速）
        └──(全滅時)──→ Firebase伝言板でSDP交換 ──→ WebRTC P2P直結 ──→ 同サーバー  ← 外出先
```

- **Firebase Realtime DB** = SDP/ICEを交換する「伝言板」だけ（画像は通らない＝無料枠で回る）
- **STUN** = NAT越えのための住所確認（無料・Google公開STUN）
- **P2P確立後** = データチャネルで既存HTTP APIをトンネル化（画像は端末↔PCを直接流れる）
- **認証2層** = ①room_id（伝言板で出会う鍵）②トークン（DC越しに提示して初めて本を配る）

---

## ステップ 1: Firebaseプロジェクト作成

1. https://console.firebase.google.com → 「プロジェクトを追加」
2. プロジェクト名（例 `comicserver`）。Google Analyticsは無効でOK

## ステップ 2: Realtime Database を有効化

1. 左メニュー「Realtime Database」→「データベースを作成」
2. ロケーション: **asia-southeast1**（日本から近い）推奨
3. 最初は「テストモードで開始」でOK（後でルールを差し替える）

## ステップ 3: 匿名認証を有効化

1. 左メニュー「Authentication」→「始める」
2. 「Sign-in method」→「匿名」→ 有効化

## ステップ 4: Androidアプリを登録

1. プロジェクト設定（歯車）→「アプリを追加」→ Android
2. パッケージ名: **`com.comicserver.comicserver_app`**
3. `google-services.json` をダウンロード →
   `comicserver_app/android/app/google-services.json` を**差し替え**

## ステップ 5: アプリ側に値を記入

`comicserver_app/lib/firebase_options.dart` の `YOUR_...` を全て実値に:

| 項目 | 取得場所 |
|------|----------|
| `apiKey` | プロジェクト設定 → 全般 → ウェブAPIキー |
| `projectId` | プロジェクト設定 → プロジェクトID |
| `messagingSenderId` | プロジェクト設定 → プロジェクト番号 |
| `appId` | プロジェクト設定 → Androidアプリ → アプリID |
| `databaseURL` | Realtime Database → データタブ最上部のURL |

## ステップ 6: サーバー側に値を記入（開発者既定）

`manga_server_app.py` の先頭付近 `DEFAULT_FIREBASE` を記入:

```python
DEFAULT_FIREBASE = {
    "api_key":      "（ウェブAPIキー。firebase_options.dartのapiKeyと同じ）",
    "database_url": "https://xxx-default-rtdb.asia-southeast1.firebasedatabase.app",
}
```

ここに書けば**配布した全サーバーが同じ伝言板を使う**（api_keyはクライアント鍵で
非秘匿。アプリにも同梱される値なので直書きしてよい）。
特定機だけ別プロジェクトにしたい場合のみ `manga_server_config.json` の
`"firebase": {"api_key": "...", "database_url": "..."}` で上書きできる。

## ステップ 7: サーバーに aiortc をインストール

```
%KEIRI_PYTHON% -m pip install aiortc
```
（※ 開発機ではインストール済み: aiortc 1.14.0）

## ステップ 8: Realtime DB セキュリティルールを適用

テストモードのままだと誰でも全 `/rooms` を読めるので、`firebase_database_rules.json`
の内容を Firebaseコンソール → Realtime Database → ルール に貼り付けて公開。
（room_id配下を匿名認証必須に制限。room_id自体が推測不能な鍵なので十分安全）

## ステップ 9: アプリを再ビルド

Firebase値を入れた後、APKを作り直す:
```
# JAVA_HOMEを通してから
cd <comicserver_appのパス>
flutter build apk --release
```
→ `build/app/outputs/flutter-apk/app-release.apk` を `Z:\...\app-release.apk` へ配置

---

## 動作確認

1. サーバー起動 → ログに `[WebRTC] Firebase接続完了。セッション待機中...` が出ればOK
   - 出ない場合: `[WebRTC] Firebase未設定...` ＝ ステップ6未記入 /
     `[WebRTC] aiortcが...` ＝ ステップ7未実施 /
     `[WebRTC] Firebase認証失敗` ＝ ステップ3未実施 or api_key誤り
2. スマホで一度**家のWi-Fiに繋いで**アプリ起動 →「LAN内を探す」でペアリング
   （room_id・トークンが自動で渡る）
3. スマホをモバイル回線に切替 → アプリ再起動
   → HTTP直結が全滅 → 自動で WebRTC にフォールバック → 本棚が開けば成功
   - サーバーログに `[WebRTC] P2P接続確立` が出る

## トラブルシューティング

- **繋がらない（STUNで抜けられないNAT）**: 設定で自前/有料のTURNサーバーを挿す。
  現状アプリ側は `turn_url`/`turn_username`/`turn_credential` を SharedPreferences から
  読む実装（UI欄は今後追加）。サーバー側は config.json の `"turn"` に記入。
- **APKが大きい（94.7MB）**: WebRTCネイティブlibを全ABI同梱のため。
  `flutter build apk --split-per-abi` で arm64単体（〜40MB）に分割可能。
