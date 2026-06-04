import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/book.dart';

class ApiService {
  String baseUrl;
  String token;              // 認証トークン（LANペアリングで受け取る or 手動入力）
  List<String> candidates;   // 接続先候補（LAN直 / IPv6 など）。失敗時に繋ぎ直すのに使う
  bool _reconnecting = false;

  ApiService({
    required this.baseUrl,
    required this.token,
    List<String>? candidates,
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

  /// セッション中に経路が変わっても追従する。現行baseUrlを確認し、
  /// ダメなら候補をレースし直して baseUrl を差し替える。
  Future<bool> ensureConnected() async {
    if (await _ping(baseUrl)) return true;
    if (_reconnecting) return false;   // 同時多発リクエストは1本だけ繋ぎ直す
    _reconnecting = true;
    try {
      final working = await resolveBaseUrl(candidates, token);
      if (working != null) { baseUrl = working; return true; }
      return false;
    } finally {
      _reconnecting = false;
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
      return await http.get(u(), headers: headers)
          .timeout(const Duration(seconds: 8));
    } catch (_) {
      if (await ensureConnected()) {
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
