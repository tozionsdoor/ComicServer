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
    apiKey: 'AIzaSyCHz05od7Ta6wJFKSWcTisWfhJh_kg1fKQ',
    projectId: 'comicserver',
    messagingSenderId: '25817336486',
    appId: '1:25817336486:android:81a92877c2ee8b4cfc8267',
    databaseURL:
        'https://comicserver-default-rtdb.asia-southeast1.firebasedatabase.app',
  );
}
