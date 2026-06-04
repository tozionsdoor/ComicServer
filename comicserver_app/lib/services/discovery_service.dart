import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// LAN自動発見で見つかったサーバー1台分の情報。
class DiscoveredServer {
  final String name;
  final String host;    // LAN内IPv4
  final int    port;    // TCPポート
  final String ipv6;    // グローバルIPv6（外出先用、無ければ空）
  final String token;   // 認証トークン（LANペアリングで自動受け渡し、無ければ空）
  final String roomId;  // WebRTC部屋ID（LANペアリングで自動受け渡し、無ければ空）

  DiscoveredServer({
    required this.name,
    required this.host,
    required this.port,
    required this.ipv6,
    required this.token,
    required this.roomId,
  });

  String get baseUrl => 'http://$host:$port';
}

/// 発見用の固定UDPポート（サーバーの DISCOVERY_PORT と一致させること）。
const int kDiscoveryPort = 8770;
const String _kProbe = 'COMICSERVER_DISCOVER';

/// LANにブロードキャストを投げ、応答したサーバー一覧を返す。
/// 同じWi-Fiに繋がっている前提。失敗しても例外は投げず空リストを返す。
Future<List<DiscoveredServer>> discoverServers({
  Duration timeout = const Duration(seconds: 2),
}) async {
  final found = <String, DiscoveredServer>{};
  RawDatagramSocket? socket;
  try {
    socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
    socket.broadcastEnabled = true;

    socket.listen((event) {
      if (event != RawSocketEvent.read) return;
      final dg = socket!.receive();
      if (dg == null) return;
      try {
        final m = jsonDecode(utf8.decode(dg.data)) as Map<String, dynamic>;
        if (m['service'] != 'comicserver') return;
        final s = DiscoveredServer(
          name:   (m['name'] ?? 'ComicServer').toString(),
          host:   (m['host'] ?? dg.address.address).toString(),
          port:   (m['port'] as num?)?.toInt() ?? 8765,
          ipv6:   (m['ipv6'] ?? '').toString(),
          token:  (m['token'] ?? '').toString(),
          roomId: (m['room_id'] ?? '').toString(),
        );
        found['${s.host}:${s.port}'] = s;
      } catch (_) {/* 不正な応答は無視 */}
    });

    // パケットロス対策に数回投げる
    final probe = utf8.encode(_kProbe);
    final dest = InternetAddress('255.255.255.255');
    for (int i = 0; i < 3; i++) {
      socket.send(probe, dest, kDiscoveryPort);
      await Future.delayed(const Duration(milliseconds: 200));
    }
    await Future.delayed(timeout);
  } catch (_) {/* バインド失敗等は空で返す */} finally {
    socket?.close();
  }
  return found.values.toList();
}
