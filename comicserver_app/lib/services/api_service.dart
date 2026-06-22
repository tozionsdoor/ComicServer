import 'dart:async';
import 'dart:convert';
import 'package:firebase_database/firebase_database.dart';
import 'package:flutter/foundation.dart';   // ValueNotifier
import 'package:flutter_cache_manager/flutter_cache_manager.dart';
import 'package:http/http.dart' as http;
import '../models/book.dart';
import 'firebase_signaling.dart';
import 'http_pinned_client.dart';
import 'webrtc_service.dart';

/// HttpFileService にダウンロードタイムアウトを付与するラッパー。
/// flutter_cache_manager のデフォルトはタイムアウト無し（ハングが永続する）。
class _TimeoutFileService implements FileService {
  final FileService _inner;
  const _TimeoutFileService(this._inner);

  @override
  int get concurrentFetches => _inner.concurrentFetches;

  @override
  set concurrentFetches(int value) => _inner.concurrentFetches = value;

  @override
  Future<FileServiceResponse> get(String url, {Map<String, String>? headers}) =>
      _inner.get(url, headers: headers).timeout(const Duration(seconds: 10));
}

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

  late final http.Client _client;
  late final CacheManager cacheManager;

  Future<bool>? _reconnectInFlight;   // 同時多発の繋ぎ直しを1本に集約
  StreamSubscription<Map<String, dynamic>>? _hostWatcher; // 直結アップグレード監視
  bool _upgrading = false;            // _onHostUpdate の多重実行防止

  // 安定維持のための状態（不安定モバイル回線対策）。
  DateTime _lastHostFetch =
      DateTime.fromMillisecondsSinceEpoch(0); // Firebase host読取スロットルの基点
  int _httpFailStreak = 0;            // 直結HTTPの連続全滅回数（WebRTC転落のヒステリシス）

  ApiService({
    required this.baseUrl,
    required this.token,
    List<String>? candidates,
    this.roomId,
    this.turnUrl,
    this.turnUsername,
    this.turnCredential,
    this.viaWebRtc = false,
    String certFingerprint = '',
  }) : candidates =
            (candidates == null || candidates.isEmpty) ? [baseUrl] : candidates {
    _client = makePinnedClient(certFingerprint);
    cacheManager = CacheManager(Config(
      'comicPageCache',
      stalePeriod: const Duration(days: 90),
      maxNrOfCacheObjects: 2000,
      fileService: _TimeoutFileService(
          HttpFileService(httpClient: makePinnedClient(certFingerprint))),
    ));
    if (viaWebRtc) _startHostWatcher();
  }

  void dispose() => _stopHostWatcher();

  String get _auth => 'Bearer $token';

  Map<String, String> get headers => {'Authorization': _auth};

  String coverUrl(String bookId) => '$baseUrl/api/books/$bookId/cover';
  String pageUrl(String bookId, int page) =>
      '$baseUrl/api/books/$bookId/pages/$page';

  Future<bool> testConnection() async {
    try {
      final res = await _client
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
    String certFingerprint = '',
    Duration timeout = const Duration(seconds: 4),
  }) async {
    if (candidates.isEmpty) return null;
    final auth = 'Bearer $token';
    final completer = Completer<String?>();
    int remaining = candidates.length;

    for (final base in candidates) {
      () async {
        String? ok;
        final client = makePinnedClient(certFingerprint);
        try {
          final res = await client
              .get(Uri.parse('$base/api/status'),
                  headers: {'Authorization': auth})
              .timeout(timeout);
          if (res.statusCode == 200) ok = base;
        } catch (_) {/* この候補は不通 */} finally {
          client.close();
        }
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
    // 現行経路が生きていれば張り直さない（resume時の無駄な再接続・バナー点滅を防ぐ）。
    // モバイル回線の一時的な輻輳で1回pingが落ちても経路は生きていることが多いので、
    // 寛容な死活判定（数回試行）で「本当に死んだ」時だけ繋ぎ直しへ進む。
    final wasDirect = !viaWebRtc;
    if (!_transportDead && await _pingResilient(baseUrl)) {
      _httpFailStreak = 0;           // 直結が生きていた → 転落カウンタをリセット
      return true;
    }

    status.value = '再接続中…';
    try {
      // 1) HTTP候補（LAN/IPv6）をレース。自宅Wi-Fiに戻った等で勝つ。
      // 画面復帰直後はOS（Samsung等のバッテリー最適化）がバックグラウンド通信を
      // まだ解除し切っていない場合があるため、1回失敗しても即WebRTCに倒さず
      // 少し待ってもう一度だけレースし直す（無駄なP2P切替・不安定化を防ぐ）。
      var working = await resolveBaseUrl(candidates, token);
      if (working == null) {
        await Future.delayed(const Duration(seconds: 2));
        working = await resolveBaseUrl(candidates, token);
      }
      // 2) 持っている候補が全滅 → Firebaseに問い合わせて最新IPv6/IPv4を取得し再レース。
      // PCのIPv6は数時間おきにローテーションする(Windowsのプライバシー一時アドレス)ため、
      // 接続中に変わっていると古い候補は本当に届かない。サーバーは60秒ごとに最新値を
      // Firebaseへ書き込んでいるので、ここで読みに行けばWebRTC交渉なしに直結を保てる。
      // ただしFirebase読み取りはダウンロード課金が発生するため、不安定回線で毎回の失敗ごとに
      // 叩かないよう最短60秒間隔にスロットルする（IPローテーションは数時間周期なので十分）。
      if (working == null && (roomId ?? '').isNotEmpty) {
        final now = DateTime.now();
        if (now.difference(_lastHostFetch) > const Duration(seconds: 60)) {
          _lastHostFetch = now;
          final host = await FirebaseSignaling.readServerHost(roomId!);
          if (host != null) {
            final freshCands = _candidatesFromHost(host);
            for (final c in freshCands) {
              if (!candidates.contains(c)) candidates.add(c);
            }
            if (freshCands.isNotEmpty) {
              working = await resolveBaseUrl(freshCands, token);
            }
          }
        }
      }
      if (working != null) {
        if (viaWebRtc) await WebRtcService.instance.dispose();  // 古いP2Pを片付け
        baseUrl = working;
        viaWebRtc = false;
        _httpFailStreak = 0;
        _stopHostWatcher();
        return true;
      }

      // 3) HTTP直結が取れなかった。WebRTCへ倒すのはヒステリシス付き。
      // 直結中の一時的な不調で即WebRTCに落ちると体感が不安定になるため、
      // 直結からの転落は「連続3回失敗」して初めて許可する（それまでは現状の直結URLを
      // 据え置き、回線が戻れば次回の_pingResilientで復帰＝WebRTC交渉もFirebaseも使わない）。
      // 既にWebRTC上で死んでいた場合（wasDirect=false）は猶予なく即再確立する。
      _httpFailStreak++;
      final allowWebRtc = !wasDirect || _httpFailStreak >= 3;
      if (allowWebRtc && (roomId ?? '').isNotEmpty) {
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
          _httpFailStreak = 0;
          _startHostWatcher(); // IPv4/IPv6が復活したら自動で直結に切り替える
          return true;
        }
      }
      return false;
    } finally {
      status.value = null;   // バナーを消す（失敗時は呼び出し側がエラー表示を出す）
    }
  }

  /// モバイル回線の一時的な輻輳で1回のpingが落ちても経路自体は生きていることが多い。
  /// 短い間隔で数回試し、全滅した時だけ「経路が死んだ」と判定する（誤った再接続を防ぐ）。
  Future<bool> _pingResilient(String base) async {
    for (int i = 0; i < 3; i++) {
      if (await _ping(base)) return true;
      if (i < 2) await Future.delayed(const Duration(milliseconds: 600));
    }
    return false;
  }

  // ── 直結アップグレード監視 ──────────────────────────────────────────────────
  // WebRTC接続中にサーバーがFirebaseの rooms/{id}/host を更新したとき、
  // IPv4/IPv6直結が使えるなら静かにHTTPへ切り替える。
  // Firebase onValue はSSEプッシュ型（ポーリングではない）。

  void _startHostWatcher() {
    if ((roomId ?? '').isEmpty) return;
    _hostWatcher?.cancel();
    final sig = FirebaseSignaling(FirebaseDatabase.instance, roomId!);
    _hostWatcher = sig.watchHost().listen(_onHostUpdate);
  }

  void _stopHostWatcher() {
    _hostWatcher?.cancel();
    _hostWatcher = null;
    _upgrading = false;
  }

  /// Firebaseのhostレコード({ipv6, ipv4, ipv4_port})からHTTP候補URLを組み立てる。
  List<String> _candidatesFromHost(Map<String, dynamic> host) {
    final ipv6 = (host['ipv6'] ?? '').toString();
    final ipv4 = (host['ipv4'] ?? '').toString();
    final p4   = (host['ipv4_port'] as num?)?.toInt() ?? 0;
    final port = _portFromCandidates();
    return <String>[
      if (ipv6.isNotEmpty) 'https://[$ipv6]:$port',
      if (ipv4.isNotEmpty && p4 > 0) 'https://$ipv4:$p4',
    ];
  }

  Future<void> _onHostUpdate(Map<String, dynamic> host) async {
    if (!viaWebRtc) { _stopHostWatcher(); return; }
    if (_upgrading) return;
    _upgrading = true;
    try {
      final newCands = _candidatesFromHost(host);
      // 元のLAN/直結候補も一緒にレースする。これが無いと、画面ON直後の一時的な
      // 通信制限などで一度WebRTCに落ちた後、同じLANに戻ってもFirebaseのhost更新
      // （IPv6/IPv4グローバルのみ）でしか直結に戻れず、LANへは永久に戻れなくなる。
      for (final c in candidates) {
        if (!newCands.contains(c)) newCands.add(c);
      }
      if (newCands.isEmpty) return;

      final working = await resolveBaseUrl(newCands, token);
      if (working == null || !viaWebRtc) return;

      await WebRtcService.instance.dispose();
      baseUrl   = working;
      viaWebRtc = false;
      for (final c in newCands) {
        if (!candidates.contains(c)) candidates.add(c);
      }
      _stopHostWatcher();
      status.value = '直結接続に切り替えました';
      Future.delayed(const Duration(seconds: 3), () => status.value = null);
    } finally {
      _upgrading = false;
    }
  }

  int _portFromCandidates() {
    for (final c in candidates) {
      final u = Uri.tryParse(c);
      if (u != null && u.hasPort) return u.port;
    }
    return 8765;
  }
  // ──────────────────────────────────────────────────────────────────────────

  Future<bool> _ping(String base) async {
    try {
      final res = await _client
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
      final res = await _client.get(u(), headers: headers)
          .timeout(const Duration(seconds: 8));
      // WebRTC切断後はローカルプロキシが404を返す（例外でないので個別に検知）。
      // 経路が死んでいる時の404だけ「繋ぎ直し」シグナルとして扱う。
      if (res.statusCode == 404 && _transportDead && await reconnect()) {
        return _client.get(u(), headers: headers)
            .timeout(const Duration(seconds: 8));
      }
      return res;
    } catch (_) {
      if (await reconnect()) {
        return _client.get(u(), headers: headers)
            .timeout(const Duration(seconds: 8));
      }
      rethrow;
    }
  }

  /// 通信失敗時に1回だけ繋ぎ直して再試行するPOST。
  Future<http.Response> _postWithRecovery(String pathAndQuery) async {
    Uri u() => Uri.parse('$baseUrl$pathAndQuery');
    try {
      final res = await _client.post(u(), headers: headers)
          .timeout(const Duration(seconds: 8));
      // WebRTC切断後はローカルプロキシが404を返す（例外でないので個別に検知）。
      if (res.statusCode == 404 && _transportDead && await reconnect()) {
        return _client.post(u(), headers: headers)
            .timeout(const Duration(seconds: 8));
      }
      return res;
    } catch (_) {
      if (await reconnect()) {
        return _client.post(u(), headers: headers)
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
  /// 新たに判明したIPv6は今回のセッションの候補リストにも反映する
  /// （次回起動を待たずに reconnect() でレースに参加できるように）。
  Future<Map<String, dynamic>?> getConnectionInfo() async {
    try {
      final res = await _getWithRecovery('/api/connection-info');
      if (res.statusCode == 200) {
        final info = jsonDecode(res.body) as Map<String, dynamic>;
        final v6 = (info['ipv6'] ?? '').toString();
        if (v6.isNotEmpty) {
          final p = Uri.tryParse(baseUrl);
          final port = (p != null && p.hasPort) ? p.port : 8765;
          final url = 'https://[$v6]:$port';
          if (!candidates.contains(url)) candidates.add(url);
        }
        final g4 = (info['ipv4_global'] ?? '').toString();
        if (g4.isNotEmpty) {
          // 外部ポートはサーバーがUPnPで実際に開けた値（内部ポートと異なることがある）
          final p4 = (info['ipv4_port'] as num?)?.toInt() ?? 0;
          final p = Uri.tryParse(baseUrl);
          final port = p4 > 0 ? p4 : ((p != null && p.hasPort) ? p.port : 8765);
          final url = 'https://$g4:$port';
          if (!candidates.contains(url)) candidates.add(url);
        }
        return info;
      }
    } catch (_) {}
    return null;
  }

  /// サーバー側の蔵書再スキャンを実行し、登録冊数を返す。
  Future<int> rescan() async {
    final res = await _postWithRecovery('/api/scan');
    if (res.statusCode != 200) throw Exception('HTTP ${res.statusCode}');
    final data = jsonDecode(res.body);
    if (data is Map && data['books'] is num) {
      return (data['books'] as num).toInt();
    }
    throw Exception('Invalid response');
  }
}

/// 保存済み設定から接続候補URLを優先順に作る（重複は除去）。
/// 1) primaryUrl（LAN直 / 手動。VPN接続中もLAN IPで到達できる）
/// 2) [ipv6]:port（IPv6が使える回線で勝つ）
/// 3) ipv4Global:ipv4Port（PPPoE等でグローバルIPv4があり、UPnPでポート開放済みの回線で勝つ。
///    外部ポートはサーバーが実際に開けた値で、内部ポートと異なることがある＝指定なければprimaryのport）
/// 遠隔時の最後の保険は Phase 2 で WebRTC 候補として足す。
List<String> buildCandidates({
  required String primaryUrl,
  String? ipv6,
  String? ipv4Global,
  int? ipv4Port,
}) {
  final list = <String>[];
  void add(String? u) {
    if (u == null || u.isEmpty) return;
    final v = u.replaceAll(RegExp(r'/+$'), '');
    if (!list.contains(v)) list.add(v);
  }

  // http:// で保存された旧 URL を https:// に正規化
  String toHttps(String u) =>
      u.startsWith('http://') ? u.replaceFirst('http://', 'https://') : u;

  add(toHttps(primaryUrl));
  if (ipv6 != null && ipv6.isNotEmpty) {
    final p = Uri.tryParse(primaryUrl);
    final port = (p != null && p.hasPort) ? p.port : 8765;
    add('https://[$ipv6]:$port');
  }
  if (ipv4Global != null && ipv4Global.isNotEmpty) {
    final p = Uri.tryParse(primaryUrl);
    final defPort = (p != null && p.hasPort) ? p.port : 8765;
    final port = (ipv4Port != null && ipv4Port > 0) ? ipv4Port : defPort;
    add('https://$ipv4Global:$port');
  }
  return list;
}
