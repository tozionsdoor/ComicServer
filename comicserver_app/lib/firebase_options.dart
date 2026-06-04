// Firebase設定ファイル — Firebaseコンソールでプロジェクト作成後に値を差し替える。
// 手順: Firebaseコンソール → プロジェクト設定 → アプリを追加 → Android
// → google-services.json をダウンロードして android/app/ に配置し、
//   下記の値を google-services.json の内容に合わせて更新する。
//
// TODO: 下記の 'YOUR_...' を実際の値に差し替えること。

import 'package:firebase_core/firebase_core.dart';

class DefaultFirebaseOptions {
  static FirebaseOptions get currentPlatform => android;

  static const FirebaseOptions android = FirebaseOptions(
    // Firebaseコンソール → プロジェクト設定 → 全般 → ウェブAPIキー
    apiKey: 'YOUR_WEB_API_KEY',
    // Firebaseコンソール → プロジェクト設定 → 全般 → プロジェクトID
    projectId: 'YOUR_PROJECT_ID',
    // Firebaseコンソール → プロジェクト設定 → 全般 → プロジェクト番号
    messagingSenderId: 'YOUR_PROJECT_NUMBER',
    // Firebaseコンソール → プロジェクト設定 → Android アプリ → アプリID
    appId: '1:YOUR_PROJECT_NUMBER:android:YOUR_APP_ID',
    // Firebaseコンソール → Realtime Database → データ タブの最上部のURL
    databaseURL: 'YOUR_DATABASE_URL',
  );
}
