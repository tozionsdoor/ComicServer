import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';   // ValueNotifier
import 'package:http/http.dart' as http;
import '../models/book.dart';
import 'webrtc_service.dart';

class ApiService {
  String baseUrl;
  String token;              // 認証トークン（LANペアリングで受け取る or 手動入力）
  List<String> candidates;   // HTTP接続先候補（LAN直 / IPv6 など）。繋ぎ直しでレースする
  bool viaWebRtc;            // 現在 baseUrl がWebRTCローカルプロキシ(loopback)を指しているか

  // WebRTC P2P を張り直すための材料（prefsから渡す）。HTTP全滅時の保険。
  String? roomId;
  String? turnUrl;
  String? turnUsername;
  String? turnCredential;

  /// 再接続中に画面下部へ出すステータス文言。null=非表示。UIが購読する。
  final ValueNotifier<String?> status = ValueNotifier<String?>(null);

  Future<bool>? _reconnectInFlight;   // 同時多発の繋ぎ直しを1本に集約

  ApiService({
    required this.baseUrl,
    required this.token,
    List<String>? candidates,
    this.roomId,
    this.turnUrl,
    this.turnUsername,
    this.turnCredential,
    this.viaWebRtc = false,
  }) : candidates =
            (candidates == null || candidates.isEmpty) ? [baseUrl] : candidates;

  String get _auth => 'Bearer $token';

  Map<String, String> get headers => {'Authorization': _auth};

  String coverUrl(String bookId) => '$baseUrl/api/books/$bookId/cover';
  String pageUrl(String bookId, int page) =>
      '$baseUrl/api/books/$bookId/pages/$page';

  Future<bool> testConnection() async {
    try {
      final res = await http
          .get(Uri.parse('$baseUrl/api/status'), headers: headers)
          .timeout(const Duration(seconds: 5));
      return res.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  /// 候補 baseUrl 群に /api/status を同時に投げ、最初に成功したものを返す（Happy Eyeballs）。
  /// 家ではLAN直、外出先ではIPv6/VPN が自動的に勝つ。全滅なら null。
  static Future<String?> resolveBaseUrl(
    List<String> candidates,
    String token, {
    Duration timeout = const Duration(seconds: 4),
  }) async {
    if (candidates.isEmpty) return null;
    final auth = 'Bearer $token';
    final completer = Completer<String?>();
    int remaining = candidates.length;

    for (final base in candidates) {
      () async {
        String? ok;
        try {
          final res = await http
              .get(Uri.parse('$base/api/status'),
                  headers: {'Authorization': auth})
              .timeout(timeout);
          if (res.statusCode == 200) ok = base;
        } catch (_) {/* この候補は不通 */}
        if (ok != null) {
          if (!completer.isCompleted) completer.complete(ok); // 最初の成功で確定
        } else {
          remaining--;
          if (remaining == 0 && !completer.isCompleted) completer.complete(null);
        }
      }();
    }
    return completer.future;
  }

  /// WebRTC経路が死んでいるか（ローカルプロキシは生きていても切断後は404を返す）。
  bool get _transportDead => viaWebRtc && !WebRtcService.instance.isConnected;

  /// 経路が死んでいれば HTTP候補→WebRTC の順で繋ぎ直す。生きていれば即true。
  /// バックグラウンド復帰時や通信失敗時に呼ぶ（常駐せずオンデマンド）。
  /// 同時に何度呼ばれても張り直しは1回に集約する。
  Future<bool> reconnect() {
    final inflight = _reconnectInFlight;
    if (inflight != null) return inflight;
    final f = _doReconnect();
    _reconnectInFlight = f;
    f.whenComplete(() => _reconnectInFlight = null);
    return f;
  }

  Future<bool> _doReconnect() async {
    // 現行経路が生きていれば張り直さない（resume時の無駄な再接続・バナー点滅を防ぐ）
    if (!_transportDead && await _ping(baseUrl)) return true;

    status.value = '再接続中…';
    try {
      // 1) HTTP候補（LAN/IPv6）をレース。自宅Wi-Fiに戻った等で勝つ。
      final working = await resolveBaseUrl(candidates, token);
      if (working != null) {
        if (viaWebRtc) await WebRtcService.instance.dispose();  // 古いP2Pを片付け
        baseUrl = working;
        viaWebRtc = false;
        return true;
      }
      // 2) WebRTC P2P を張り直す（loopbackポートは変わるのでbaseUrlを差し替え）
      if ((roomId ?? '').isNotEmpty) {
        status.value = 'P2Pで再接続中…';
        final localUrl = await WebRtcService.instance.connect(
          roomId: roomId!,
          authToken: token,
          turnUrl: turnUrl,
          turnUsername: turnUsername,
          turnCredential: turnCredential,
        );
        if (localUrl != null) {
          baseUrl = localUrl;
          viaWebRtc = true;
          return true;
        }
      }
      return false;
    } finally {
      status.value = null;   // バナーを消す（失敗時は呼び出し側がエラー表示を出す）
    }
  }

  Future<bool> _ping(String base) async {
    try {
      final res = await http
          .get(Uri.parse('$base/api/status'), headers: headers)
          .timeout(const Duration(seconds: 3));
      return res.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  /// 通信失敗時に1回だけ繋ぎ直して再試行するGET。
  Future<http.Response> _getWithRecovery(String pathAndQuery) async {
    Uri u() => Uri.parse('$baseUrl$pathAndQuery');
    try {
      final res = await http.get(u(), headers: headers)
          .timeout(const Duration(seconds: 8));
      // WebRTC切断後はローカルプロキシが404を返す（例外でないので個別に検知）。
      // 経路が死んでいる時の404だけ「繋ぎ直し」シグナルとして扱う。
      if (res.statusCode == 404 && _transportDead && await reconnect()) {
        return http.get(u(), headers: headers)
            .timeout(const Duration(seconds: 8));
      }
      return res;
    } catch (_) {
      if (await reconnect()) {
        return http.get(u(), headers: headers)
            .timeout(const Duration(seconds: 8));
      }
      rethrow;
    }
  }

  Future<FolderContents> getFolders(String path) async {
    final res = await _getWithRecovery(
        '/api/folders?path=${Uri.encodeQueryComponent(path)}');
    if (res.statusCode != 200) throw Exception('HTTP ${res.statusCode}');
    return FolderContents.fromJson(jsonDecode(res.body));
  }

  /// 全冊リスト（id/title/rel）。フォルダ横断の検索に使う。
  Future<List<BookItem>> getBooks() async {
    final res = await _getWithRecovery('/api/books');
    if (res.statusCode != 200) throw Exception('HTTP ${res.statusCode}');
    final list = jsonDecode(res.body) as List;
    return list
        .map((e) => BookItem.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<BookInfo> getBookInfo(String bookId) async {
    final res = await _getWithRecovery('/api/books/$bookId/info');
    if (res.statusCode != 200) throw Exception('HTTP ${res.statusCode}');
    return BookInfo.fromJson(jsonDecode(res.body));
  }

  /// 接続情報（ipv6 など）を取得。次回接続のため保存に使う。
  Future<Map<String, dynamic>?> getConnectionInfo() async {
    try {
      final res = await _getWithRecovery('/api/connection-info');
      if (res.statusCode == 200) {
        return jsonDecode(res.body) as Map<String, dynamic>;
      }
    } catch (_) {}
    return null;
  }
}

/// 保存済み設定から接続候補URLを優先順に作る（重複は除去）。
/// 1) primaryUrl（LAN直 / 手動。VPN接続中もLAN IPで到達できる）
/// 2) [ipv6]:port（IPv6が使える回線で勝つ）
/// 遠隔時の最後の保険は Phase 2 で WebRTC 候補として足す。
List<String> buildCandidates({
  required String primaryUrl,
  String? ipv6,
}) {
  final list = <String>[];
  void add(String? u) {
    if (u == null || u.isEmpty) return;
    final v = u.replaceAll(RegExp(r'/+$'), '');
    if (!list.contains(v)) list.add(v);
  }

  add(primaryUrl);
  if (ipv6 != null && ipv6.isNotEmpty) {
    final p = Uri.tryParse(primaryUrl);
    final port = (p != null && p.hasPort) ? p.port : 8765;
    add('http://[$ipv6]:$port');
  }
  return list;
}
