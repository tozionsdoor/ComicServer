import 'dart:async';
import 'package:firebase_database/firebase_database.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';

/// Firebase Realtime DB を使ったWebRTCシグナリング（Androidアプリ側 = Caller）。
/// 非トリクルICE方式: ICE候補はoffer/answerのSDPに埋め込むため、候補の個別交換は行わない。
/// データ構造:
///   /rooms/{roomId}/sessions/{sessionId}/
///     offer:  {sdp, type}   （アプリが書く。候補埋め込み済み）
///     answer: {sdp, type}   （PCサーバーが書く。候補埋め込み済み）
class FirebaseSignaling {
  final FirebaseDatabase _db;
  final String roomId;

  FirebaseSignaling(this._db, this.roomId);

  DatabaseReference _sessionRef(String sessionId) =>
      _db.ref('rooms/$roomId/sessions/$sessionId');

  /// Offer SDPをFirebaseに書き込む。
  Future<void> sendOffer(String sessionId, RTCSessionDescription offer) async {
    await _sessionRef(sessionId).child('offer').set({
      'sdp': offer.sdp,
      'type': offer.type,
    });
  }

  /// PCサーバーからのAnswerを待つ（タイムアウト: 30秒）。
  Future<RTCSessionDescription?> waitForAnswer(String sessionId) async {
    final ref = _sessionRef(sessionId).child('answer');
    final completer = Completer<RTCSessionDescription?>();

    final sub = ref.onValue.listen((event) {
      final val = event.snapshot.value;
      if (val is Map) {
        completer.complete(
          RTCSessionDescription(val['sdp']?.toString(), val['type']?.toString()),
        );
      }
    });

    try {
      return await completer.future.timeout(const Duration(seconds: 30),
          onTimeout: () => null);
    } finally {
      await sub.cancel();
    }
  }

  /// セッションレコードを削除（切断後のクリーンアップ）。
  Future<void> deleteSession(String sessionId) async {
    await _sessionRef(sessionId).remove();
  }

  /// サーバーが登録した最新のグローバルIPv6アドレスをFirebaseから取得する。
  /// 取得失敗・未登録の場合は null を返す（接続候補なしとして扱う）。
  static Future<String?> readServerIpv6(
      FirebaseDatabase db, String roomId) async {
    try {
      final snap = await db
          .ref('rooms/$roomId/host/ipv6')
          .get()
          .timeout(const Duration(seconds: 4));
      final v = snap.value;
      return (v is String && v.isNotEmpty) ? v : null;
    } catch (_) {
      return null;
    }
  }
}
