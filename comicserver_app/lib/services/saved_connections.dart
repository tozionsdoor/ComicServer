import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';

const _prefsKey = 'saved_connections';
const _maxSaved = 10;

/// 過去に接続成功したサーバーURLと、それに紐づく認証トークン・接続情報の組。
///
/// 証明書フィンガープリント・IPv6・ルームIDなどはサーバーごとに違うので、
/// URLと一緒に覚えておかないと、履歴から別のサーバーへ切り替えた時に
/// 直前のサーバーの値で繋ごうとして必ず失敗する（証明書のピン留めで弾かれる）。
class SavedConnection {
  final String url;
  final String token;
  final String certFingerprint;
  final String ipv6;
  final String ipv4Global;
  final int    ipv4Port;
  final String roomId;
  /// 接続情報つきで保存された履歴か。旧バージョンが保存した履歴は url/token だけ。
  final bool   hasMeta;

  const SavedConnection({
    required this.url,
    required this.token,
    this.certFingerprint = '',
    this.ipv6 = '',
    this.ipv4Global = '',
    this.ipv4Port = 0,
    this.roomId = '',
    this.hasMeta = false,
  });

  Map<String, dynamic> toJson() => {
        'url': url,
        'token': token,
        if (hasMeta) ...{
          'cert_fingerprint': certFingerprint,
          'ipv6': ipv6,
          'ipv4_global': ipv4Global,
          'ipv4_port': ipv4Port,
          'room_id': roomId,
        },
      };

  factory SavedConnection.fromJson(Map<String, dynamic> j) => SavedConnection(
        url: j['url'] as String? ?? '',
        token: j['token'] as String? ?? '',
        certFingerprint: j['cert_fingerprint'] as String? ?? '',
        ipv6: j['ipv6'] as String? ?? '',
        ipv4Global: j['ipv4_global'] as String? ?? '',
        ipv4Port: (j['ipv4_port'] as num?)?.toInt() ?? 0,
        roomId: j['room_id'] as String? ?? '',
        hasMeta: j.containsKey('cert_fingerprint'),
      );
}

/// 接続履歴（URL⇔トークン・接続情報）の永続化。SharedPreferencesにJSON配列で保存する。
class SavedConnectionsStore {
  static Future<List<SavedConnection>> load() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_prefsKey);
    if (raw == null || raw.isEmpty) return [];
    try {
      final list = jsonDecode(raw) as List<dynamic>;
      return list
          .map((e) => SavedConnection.fromJson(e as Map<String, dynamic>))
          .where((c) => c.url.isNotEmpty)
          .toList();
    } catch (_) {
      return [];
    }
  }

  /// 接続成功時に呼ぶ。同じURLは上書きし最新として先頭に移動、最大件数を超えたら古いものを捨てる。
  static Future<List<SavedConnection>> upsert(
    String url,
    String token, {
    required String certFingerprint,
    required String ipv6,
    required String ipv4Global,
    required int ipv4Port,
    required String roomId,
  }) async {
    if (url.isEmpty || token.isEmpty) return load();
    final prefs = await SharedPreferences.getInstance();
    final current = await load();
    final updated = [
      SavedConnection(
        url: url,
        token: token,
        certFingerprint: certFingerprint,
        ipv6: ipv6,
        ipv4Global: ipv4Global,
        ipv4Port: ipv4Port,
        roomId: roomId,
        hasMeta: true,
      ),
      ...current.where((c) => c.url != url),
    ].take(_maxSaved).toList();
    await prefs.setString(
        _prefsKey, jsonEncode(updated.map((c) => c.toJson()).toList()));
    return updated;
  }

  static Future<List<SavedConnection>> remove(String url) async {
    final prefs = await SharedPreferences.getInstance();
    final updated = (await load()).where((c) => c.url != url).toList();
    await prefs.setString(
        _prefsKey, jsonEncode(updated.map((c) => c.toJson()).toList()));
    return updated;
  }
}
