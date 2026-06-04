import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:firebase_database/firebase_database.dart';
import 'package:flutter/foundation.dart';  // Uint8List/debugPrint を提供
import 'package:flutter_webrtc/flutter_webrtc.dart';

import 'firebase_signaling.dart';

/// WebRTC P2P接続を管理し、データチャネルを使ってAPIリクエストをPC側に転送する。
/// 接続後はローカルHTTPプロキシサーバー(127.0.0.1:port)を立ち上げ、
/// 既存の ApiService がそのまま使えるようにする。
class WebRtcService {
  static final WebRtcService _instance = WebRtcService._();
  static WebRtcService get instance => _instance;
  WebRtcService._();

  RTCPeerConnection? _pc;
  RTCDataChannel? _dc;
  HttpServer? _localServer;

  final _pending = <int, Completer<_DcResponse>>{};
  int _nextId = 1;
  bool _connected = false;

  bool get isConnected => _connected;

  /// WebRTC P2P接続を確立する。
  /// [roomId]   : Firebase上の部屋ID（LANペアリングで取得）
  /// [authToken]: ComicServerの認証トークン（接続後の認証に使う）
  /// [turnUrl/turnUsername/turnCredential]: 任意のTURNサーバー設定
  /// 戻り値: ローカルプロキシの baseUrl (例: http://127.0.0.1:PORT)、失敗なら null。
  Future<String?> connect({
    required String roomId,
    required String authToken,
    String? turnUrl,
    String? turnUsername,
    String? turnCredential,
  }) async {
    await _cleanup();
    _authToken = authToken;   // プロキシ起動前に必ずセット（リクエストに載せる認証層2）

    try {
      // Firebase匿名認証
      final auth = FirebaseAuth.instance;
      if (auth.currentUser == null) {
        await auth.signInAnonymously();
      }
      final db = FirebaseDatabase.instance;
      final signaling = FirebaseSignaling(db, roomId);
      final sessionId = db.ref().push().key!;

      // RTCPeerConnection 作成
      final iceServers = <Map<String, dynamic>>[
        {'urls': 'stun:stun.l.google.com:19302'},
      ];
      if (turnUrl != null && turnUrl.isNotEmpty) {
        iceServers.add({
          'urls': turnUrl,
          if (turnUsername != null && turnUsername.isNotEmpty)
            'username': turnUsername,
          if (turnCredential != null && turnCredential.isNotEmpty)
            'credential': turnCredential,
        });
      }
      _pc = await createPeerConnection({'iceServers': iceServers});

      // ICE収集完了を検知するための Completer（非トリクル方式）
      final gatheringDone = Completer<void>();
      _pc!.onIceGatheringState = (state) {
        if (state == RTCIceGatheringState.RTCIceGatheringStateComplete &&
            !gatheringDone.isCompleted) {
          gatheringDone.complete();
        }
      };

      // データチャネルを Caller 側で作成（ordered=true がデフォルトで信頼性あり）
      _dc = await _pc!.createDataChannel('api', RTCDataChannelInit()..ordered = true);
      _dc!.onMessage = _onMessage;

      // Offer 作成 → ICE収集完了まで待ってから送信（候補をSDPに埋め込む非トリクル方式。
      // ポーリング型シグナリングではトリクルの利点が無く、候補取りこぼしも防げる）
      final offer = await _pc!.createOffer();
      await _pc!.setLocalDescription(offer);
      try {
        await gatheringDone.future.timeout(const Duration(seconds: 10));
      } on TimeoutException {/* 集まった分だけで続行 */}

      // 収集後の完全なSDP（候補埋め込み済み）を送信
      final fullOffer = await _pc!.getLocalDescription() ?? offer;
      await signaling.sendOffer(sessionId, fullOffer);

      // PCサーバーからのAnswerを待つ（候補はSDPに埋め込み済み）
      final answer = await signaling.waitForAnswer(sessionId);
      if (answer == null) {
        await _cleanup();
        return null;
      }
      await _pc!.setRemoteDescription(answer);

      // データチャネルが開くのを待つ
      final dcReady = Completer<void>();
      _dc!.onDataChannelState = (state) {
        if (state == RTCDataChannelState.RTCDataChannelOpen && !dcReady.isCompleted) {
          dcReady.complete();
        }
      };
      if (_dc!.state == RTCDataChannelState.RTCDataChannelOpen) {
        if (!dcReady.isCompleted) dcReady.complete();
      }

      try {
        await dcReady.future.timeout(const Duration(seconds: 30));
      } on TimeoutException {
        await signaling.deleteSession(sessionId);
        await _cleanup();
        return null;
      }

      _connected = true;

      // 切断検知
      _pc!.onConnectionState = (state) {
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateFailed ||
            state == RTCPeerConnectionState.RTCPeerConnectionStateClosed ||
            state == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected) {
          _connected = false;
          debugPrint('[WebRTC] 切断: $state');
        }
      };

      // ローカルHTTPプロキシを起動
      _localServer = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      _localServer!.listen(_handleProxyRequest);
      final port = _localServer!.port;

      debugPrint('[WebRTC] P2P確立 → http://127.0.0.1:$port');
      return 'http://127.0.0.1:$port';
    } catch (e) {
      debugPrint('[WebRTC] 接続エラー: $e');
      await _cleanup();
      return null;
    }
  }

  String _authToken = '';

  void _onMessage(RTCDataChannelMessage message) {
    if (!message.isBinary) return;
    try {
      final data = message.binary;
      // ByteData.sublistView で安全にオフセット解決
      final headerLen = ByteData.sublistView(data, 0, 4).getUint32(0, Endian.big);
      final headerJson = utf8.decode(data.sublist(4, 4 + headerLen));
      final header = jsonDecode(headerJson) as Map<String, dynamic>;
      final reqId = (header['id'] as num).toInt();
      final status = (header['status'] as num).toInt();
      final contentType =
          header['content_type']?.toString() ?? 'application/octet-stream';
      final body = data.sublist(4 + headerLen);

      final completer = _pending.remove(reqId);
      completer?.complete(_DcResponse(status, contentType, body));
    } catch (e) {
      debugPrint('[WebRTC] メッセージパースエラー: $e');
    }
  }

  Future<_DcResponse> _sendRequest(Map<String, dynamic> req) async {
    final id = req['id'] as int;
    // 認証層2: 全リクエストにトークンを載せてPCサーバーに提示する。
    req['token'] = _authToken;
    final completer = Completer<_DcResponse>();
    _pending[id] = completer;
    _dc!.send(RTCDataChannelMessage(jsonEncode(req)));
    return completer.future.timeout(
      const Duration(seconds: 30),
      onTimeout: () {
        _pending.remove(id);
        throw TimeoutException('WebRTC request timeout: ${req['type']}');
      },
    );
  }

  int _newId() => _nextId++;

  /// ローカルHTTPプロキシ: アプリからのHTTPリクエストをデータチャネルに転送する。
  Future<void> _handleProxyRequest(HttpRequest request) async {
    final path = request.uri.path;
    final q = request.uri.queryParameters;

    // Authorization ヘッダを透過させる（CachedNetworkImageが送る場合）

    Map<String, dynamic>? dcReq;

    if (path == '/api/status') {
      dcReq = {'id': _newId(), 'type': 'status'};
    } else if (path == '/api/connection-info') {
      dcReq = {'id': _newId(), 'type': 'connection_info'};
    } else if (path == '/api/folders') {
      dcReq = {'id': _newId(), 'type': 'folders', 'path': q['path'] ?? ''};
    } else if (path == '/api/books') {
      dcReq = {'id': _newId(), 'type': 'books'};
    } else if (path.startsWith('/api/books/') && path.endsWith('/info')) {
      final bid = path.split('/')[3];
      dcReq = {'id': _newId(), 'type': 'book_info', 'bid': bid};
    } else if (path.startsWith('/api/books/') && path.endsWith('/cover')) {
      final bid = path.split('/')[3];
      dcReq = {'id': _newId(), 'type': 'cover', 'bid': bid};
    } else if (path.contains('/pages/')) {
      final parts = path.split('/');
      // /api/books/{bid}/pages/{n}
      final bid = parts[3];
      final n = int.tryParse(parts[5]) ?? 0;
      dcReq = {'id': _newId(), 'type': 'page', 'bid': bid, 'n': n};
    }

    if (dcReq == null || !_connected) {
      request.response.statusCode = HttpStatus.notFound;
      await request.response.close();
      return;
    }

    try {
      final resp = await _sendRequest(dcReq);
      request.response.statusCode = resp.status;
      request.response.headers.set('Content-Type', resp.contentType);
      // CORS
      request.response.headers.set('Access-Control-Allow-Origin', '*');
      request.response.add(resp.body);
      await request.response.close();
    } catch (e) {
      request.response.statusCode = HttpStatus.internalServerError;
      await request.response.close();
    }
  }

  Future<void> _cleanup() async {
    _connected = false;
    await _localServer?.close();
    _localServer = null;
    await _dc?.close();
    _dc = null;
    await _pc?.close();
    _pc = null;
    for (final c in _pending.values) {
      c.completeError(StateError('WebRTC disconnected'));
    }
    _pending.clear();
  }

  Future<void> dispose() => _cleanup();
}

class _DcResponse {
  final int status;
  final String contentType;
  final Uint8List body;
  _DcResponse(this.status, this.contentType, this.body);
}
