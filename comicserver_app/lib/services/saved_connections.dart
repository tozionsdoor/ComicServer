import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';

const _prefsKey = 'saved_connections';
const _maxSaved = 10;

/// 過去に接続成功したサーバーURLと、それに紐づく認証トークンの組。
class SavedConnection {
  final String url;
  final String token;
  const SavedConnection({required this.url, required this.token});

  Map<String, dynamic> toJson() => {'url': url, 'token': token};
  factory SavedConnection.fromJson(Map<String, dynamic> j) => SavedConnection(
        url: j['url'] as String? ?? '',
        token: j['token'] as String? ?? '',
      );
}

/// 接続履歴（URL⇔トークン）の永続化。SharedPreferencesにJSON配列で保存する。
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
  static Future<List<SavedConnection>> upsert(String url, String token) async {
    if (url.isEmpty || token.isEmpty) return load();
    final prefs = await SharedPreferences.getInstance();
    final current = await load();
    final updated = [
      SavedConnection(url: url, token: token),
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
