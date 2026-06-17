import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// LAN自動発見で見つかったサーバー1台分の情報。
class DiscoveredServer {
  final String name;
  final String host;      // LAN内IPv4
  final int    port;      // TCPポート
  final String ipv6;      // グローバルIPv6（外出先用、無ければ空）
  final String ipv4Global; // グローバルIPv4（PPPoE等＋UPnP自動開放、無ければ空）
  final int    ipv4Port;   // UPnPで開けた外部ポート（内部ポートと異なることがある。0=未開放）
  final String token;     // 旧サーバー互換（新サーバーは空）
  final String regNonce;        // 端末登録ノンス（新サーバー）
  final String roomId;          // WebRTC部屋ID
  final String certFingerprint; // TLS証明書 SHA-256 フィンガープリント（hex）

  DiscoveredServer({
    required this.name,
    required this.host,
    required this.port,
    required this.ipv6,
    this.ipv4Global      = '',
    this.ipv4Port        = 0,
    this.token           = '',
    this.regNonce        = '',
    required this.roomId,
    this.certFingerprint = '',
  });

  String get baseUrl => 'https://$host:$port';
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
          name:            (m['name']             ?? 'ComicServer').toString(),
          host:            (m['host']             ?? dg.address.address).toString(),
          port:            (m['port']             as num?)?.toInt() ?? 8765,
          ipv6:            (m['ipv6']             ?? '').toString(),
          ipv4Global:      (m['ipv4_global']      ?? '').toString(),
          ipv4Port:        (m['ipv4_port']        as num?)?.toInt() ?? 0,
          token:           (m['token']            ?? '').toString(),
          regNonce:        (m['reg_nonce']        ?? '').toString(),
          roomId:          (m['room_id']          ?? '').toString(),
          certFingerprint: (m['cert_fingerprint'] ?? '').toString(),
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
