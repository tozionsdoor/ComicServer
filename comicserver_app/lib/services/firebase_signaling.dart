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
  /// セッションノード全体を1回のsetで書く（offer子ノードだけを書くと、
  /// サーバーのSSE監視には path="/{sid}/offer" の深いイベントとして届く。
  /// 全体setなら path="/{sid}" で届き、サーバーが確実に新セッションとして拾える）。
  Future<void> sendOffer(String sessionId, RTCSessionDescription offer) async {
    await _sessionRef(sessionId).set({
      'offer': {
        'sdp': offer.sdp,
        'type': offer.type,
      },
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

  /// rooms/{roomId}/host の変化をリアルタイム監視するストリーム。
  /// WebRTC接続中に直結経路（IPv4/IPv6）が使えるようになったときアプリ側に通知するために使う。
  /// Firebase onValue はSSE型（変化時のみ受信）なので課金・通信量への影響は軽微。
  Stream<Map<String, dynamic>> watchHost() =>
      _db.ref('rooms/$roomId/host').onValue
          .where((e) => e.snapshot.value is Map)
          .map((e) {
            final v = e.snapshot.value as Map;
            return {
              'ipv6':      (v['ipv6']      ?? '').toString(),
              'ipv4':      (v['ipv4']      ?? '').toString(),
              'ipv4_port': (v['ipv4_port'] is num) ? (v['ipv4_port'] as num).toInt() : 0,
            };
          });

  /// サーバーがFirebaseに登録した最新の接続先を取得する。
  /// 返り値: {'ipv6': String, 'ipv4': String, 'ipv4_port': int}。失敗・未登録は null。
  /// 初回起動や外出先で prefs に直結情報が無いとき、WebRTCに落ちる前に
  /// IPv6/IPv4直結の候補を用意するために使う（外部ポートはサーバーがUPnPで開けた値）。
  static Future<Map<String, dynamic>?> readServerHost(String roomId) async {
    if (roomId.isEmpty) return null;
    try {
      final snap = await FirebaseDatabase.instance
          .ref('rooms/$roomId/host')
          .get()
          .timeout(const Duration(seconds: 4));
      final v = snap.value;
      if (v is Map) {
        return {
          'ipv6': (v['ipv6'] ?? '').toString(),
          'ipv4': (v['ipv4'] ?? '').toString(),
          'ipv4_port': (v['ipv4_port'] is num) ? (v['ipv4_port'] as num).toInt() : 0,
        };
      }
    } catch (_) {/* 取得失敗時は prefs の値で続行 */}
    return null;
  }
}
