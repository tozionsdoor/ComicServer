import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';
import '../services/device_service.dart';
import '../services/discovery_service.dart';
import '../services/http_pinned_client.dart';
import '../services/webrtc_service.dart';
import 'shelf_screen.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _urlCtrl   = TextEditingController(text: 'https://192.168.0.25:8765');
  final _tokenCtrl = TextEditingController();
  // TURN（任意・STUNで繋がらない少数派向け。標準は空＝STUN-only）
  final _turnUrlCtrl  = TextEditingController();
  final _turnUserCtrl = TextEditingController();
  final _turnCredCtrl = TextEditingController();
  bool _loading       = false;
  bool _discovering   = false;
  bool _obscure       = true;
  bool _showTurn      = false;
  String _error       = '';

  // 端末登録・承認待ちフロー
  bool   _waitingApproval  = false;
  String _regToken         = '';
  String _pendingBaseUrl   = '';
  String _certFingerprint  = '';   // LAN発見で取得した証明書フィンガープリント
  int    _approvalTimeout  = 180;
  Timer? _approvalTimer;

  @override
  void initState() {
    super.initState();
    _loadSaved();
  }

  @override
  void dispose() {
    _approvalTimer?.cancel();
    _urlCtrl.dispose();
    _tokenCtrl.dispose();
    _turnUrlCtrl.dispose();
    _turnUserCtrl.dispose();
    _turnCredCtrl.dispose();
    super.dispose();
  }

  Future<void> _loadSaved() async {
    final prefs = await SharedPreferences.getInstance();
    _certFingerprint = prefs.getString('cert_fingerprint') ?? '';
    setState(() {
      final saved = prefs.getString('url') ?? _urlCtrl.text;
      // http:// → https:// に正規化（サーバーTLS化後の既存保存値を修正）
      _urlCtrl.text       = saved.startsWith('http://')
          ? saved.replaceFirst('http://', 'https://')
          : saved;
      _tokenCtrl.text     = prefs.getString('token') ?? '';
      _turnUrlCtrl.text   = prefs.getString('turn_url')        ?? '';
      _turnUserCtrl.text  = prefs.getString('turn_username')   ?? '';
      _turnCredCtrl.text  = prefs.getString('turn_credential') ?? '';
    });
  }

  Future<void> _discover() async {
    setState(() { _discovering = true; _error = ''; });
    final servers = await discoverServers();
    if (!mounted) return;
    setState(() => _discovering = false);

    if (servers.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('サーバーが見つかりませんでした（同じWi-Fiに接続しているか確認してください）'),
      ));
      return;
    }

    final selected = await showModalBottomSheet<DiscoveredServer>(
      context: context,
      backgroundColor: const Color(0xFF181825),
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Padding(
              padding: EdgeInsets.all(16),
              child: Text('見つかったサーバー',
                  style: TextStyle(
                      color: Color(0xFFcdd6f4),
                      fontSize: 16,
                      fontWeight: FontWeight.bold)),
            ),
            for (final s in servers)
              ListTile(
                leading: const Icon(Icons.dns, color: Color(0xFF89b4fa)),
                title: Text(s.name,
                    style: const TextStyle(color: Color(0xFFcdd6f4))),
                subtitle: Text(
                    '${s.host}:${s.port}'
                    '${s.ipv6.isNotEmpty ? '   ・IPv6あり' : ''}',
                    style: const TextStyle(color: Color(0xFFa6adc8))),
                onTap: () => Navigator.pop(ctx, s),
              ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );

    if (selected != null && mounted) {
      if (selected.regNonce.isNotEmpty) {
        // 新サーバー: 端末登録フロー
        await _startRegistration(selected);
      } else if (selected.token.isNotEmpty) {
        // 旧サーバー互換: トークンを直接受け取る
        setState(() {
          _urlCtrl.text   = selected.baseUrl;
          _tokenCtrl.text = selected.token;
        });
        final prefs = await SharedPreferences.getInstance();
        await prefs.setString('ipv6', selected.ipv6);
        if (selected.roomId.isNotEmpty) {
          await prefs.setString('room_id', selected.roomId);
        }
      }
    }
  }

  // ── 端末登録フロー ──────────────────────────────────────────────────────────
  Future<void> _startRegistration(DiscoveredServer server) async {
    setState(() { _loading = true; _error = ''; });
    final prefs    = await SharedPreferences.getInstance();
    final deviceId = await DeviceService.getDeviceId();
    final baseUrl  = server.baseUrl;
    await prefs.setString('ipv6', server.ipv6);
    if (server.roomId.isNotEmpty) await prefs.setString('room_id', server.roomId);
    // フィンガープリントを保存し、以降の接続でピン留めに使う
    _certFingerprint = server.certFingerprint;
    if (server.certFingerprint.isNotEmpty) {
      await prefs.setString('cert_fingerprint', server.certFingerprint);
    }

    final pinnedClient = makePinnedClient(_certFingerprint);
    try {
      final res = await pinnedClient.post(
        Uri.parse('$baseUrl/api/devices/register'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'device_id':   deviceId,
          'device_name': DeviceService.deviceName,
          'reg_nonce':   server.regNonce,
        }),
      ).timeout(const Duration(seconds: 10));

      if (!mounted) return;
      final data = jsonDecode(res.body) as Map<String, dynamic>;
      if (res.statusCode == 200) {
        final status = data['status'] as String? ?? '';
        if (status == 'pending') {
          setState(() {
            _loading        = false;
            _waitingApproval = true;
            _regToken       = data['reg_token'] as String? ?? '';
            _pendingBaseUrl = baseUrl;
            _approvalTimeout = 180;
          });
          _startApprovalPolling(baseUrl, _regToken);
          return;
        }
        if (status == 'already_approved') {
          // 既承認（再ペアリング）: トークン要手動入力 or 再度LAN発見
          setState(() { _loading = false; _urlCtrl.text = baseUrl; });
          return;
        }
      }
      setState(() {
        _loading = false;
        _error   = 'ペアリングに失敗しました (${res.statusCode})';
      });
    } catch (e) {
      if (mounted) setState(() { _loading = false; _error = '接続できません: $e'; });
    } finally {
      pinnedClient.close();
    }
  }

  void _startApprovalPolling(String baseUrl, String regToken) {
    _approvalTimer?.cancel();
    _approvalTimer = Timer.periodic(const Duration(seconds: 3), (timer) async {
      if (!mounted) { timer.cancel(); return; }
      if (_approvalTimeout <= 0) {
        timer.cancel();
        setState(() {
          _waitingApproval = false;
          _error = 'タイムアウト。もう一度「LAN内のサーバーを探す」を試してください。';
        });
        return;
      }
      try {
        final client = makePinnedClient(_certFingerprint);
        try {
          final res = await client.get(
            Uri.parse('$baseUrl/api/devices/status?reg_token=${Uri.encodeComponent(regToken)}'),
          ).timeout(const Duration(seconds: 5));
          if (!mounted) { timer.cancel(); return; }
          if (res.statusCode == 200) {
            final data = jsonDecode(res.body) as Map<String, dynamic>;
            if (data['status'] == 'approved') {
              timer.cancel();
              await _saveAndNavigate(baseUrl, data['token'] as String? ?? '');
              return;
            }
          } else if (res.statusCode == 403) {
            timer.cancel();
            setState(() { _waitingApproval = false; _error = '接続リクエストが拒否されました。'; });
            return;
          }
        } finally {
          client.close();
        }
      } catch (_) { /* ネットワークエラーは無視してリトライ */ }
      if (mounted) setState(() => _approvalTimeout -= 3);
    });
  }

  Future<void> _saveAndNavigate(String baseUrl, String token) async {
    final prefs           = await SharedPreferences.getInstance();
    final ipv6            = prefs.getString('ipv6');
    final roomId          = prefs.getString('room_id') ?? '';
    final certFingerprint = prefs.getString('cert_fingerprint') ?? '';
    final candidates      = buildCandidates(primaryUrl: baseUrl, ipv6: ipv6);
    await prefs.setString('url',   baseUrl);
    await prefs.setString('token', token);
    final working = await ApiService.resolveBaseUrl(candidates, token,
        certFingerprint: certFingerprint);
    if (!mounted) return;
    final api = ApiService(
      baseUrl:          working ?? baseUrl,
      token:            token,
      candidates:       candidates,
      roomId:           roomId,
      turnUrl:          prefs.getString('turn_url'),
      turnUsername:     prefs.getString('turn_username'),
      turnCredential:   prefs.getString('turn_credential'),
      certFingerprint:  certFingerprint,
    );
    setState(() { _waitingApproval = false; });
    Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => ShelfScreen(api: api)));
  }

  void _refreshConnInfo(ApiService api) {
    api.getConnectionInfo().then((info) async {
      if (info == null) return;
      final pr = await SharedPreferences.getInstance();
      final v6 = (info['ipv6'] ?? '').toString();
      if (v6.isNotEmpty) await pr.setString('ipv6', v6);
      final rid = (info['room_id'] ?? '').toString();
      if (rid.isNotEmpty) await pr.setString('room_id', rid);
    });
  }

  Future<void> _connect() async {
    setState(() { _loading = true; _error = ''; });
    final url   = _urlCtrl.text.trim().replaceAll(RegExp(r'/$'), '');
    final token = _tokenCtrl.text.trim();

    final prefs  = await SharedPreferences.getInstance();
    // TURN設定を保存（任意。空ならSTUN-only）
    await prefs.setString('turn_url',        _turnUrlCtrl.text.trim());
    await prefs.setString('turn_username',   _turnUserCtrl.text.trim());
    await prefs.setString('turn_credential', _turnCredCtrl.text.trim());
    final ipv6            = prefs.getString('ipv6');
    final roomId          = prefs.getString('room_id') ?? '';
    final certFingerprint = prefs.getString('cert_fingerprint') ?? '';
    final candidates = buildCandidates(primaryUrl: url, ipv6: ipv6);
    final working = await ApiService.resolveBaseUrl(candidates, token,
        certFingerprint: certFingerprint);

    if (!mounted) return;
    if (working != null) {
      await prefs.setString('url',   url);
      await prefs.setString('token', token);
      if (!mounted) return;
      final api = ApiService(
          baseUrl: working, token: token, candidates: candidates,
          roomId: roomId,
          turnUrl:          prefs.getString('turn_url'),
          turnUsername:     prefs.getString('turn_username'),
          turnCredential:   prefs.getString('turn_credential'),
          certFingerprint:  certFingerprint);
      _refreshConnInfo(api);
      Navigator.pushReplacement(context,
          MaterialPageRoute(builder: (_) => ShelfScreen(api: api)));
      return;
    }

    // HTTP全滅 → WebRTC P2P フォールバック
    if (roomId.isNotEmpty) {
      setState(() { _error = 'HTTP接続失敗。WebRTC P2Pで試みています...'; });
      final webRtc = WebRtcService.instance;
      final localUrl = await webRtc.connect(
        roomId: roomId,
        authToken: token,
        turnUrl:        prefs.getString('turn_url'),
        turnUsername:   prefs.getString('turn_username'),
        turnCredential: prefs.getString('turn_credential'),
      );
      if (!mounted) return;
      if (localUrl != null) {
        await prefs.setString('url',   url);
        await prefs.setString('token', token);
        if (!mounted) return;
        final api = ApiService(
            baseUrl: localUrl, token: token, candidates: candidates,
            viaWebRtc: true, roomId: roomId,
            turnUrl:          prefs.getString('turn_url'),
            turnUsername:     prefs.getString('turn_username'),
            turnCredential:   prefs.getString('turn_credential'),
            certFingerprint:  certFingerprint);
        Navigator.pushReplacement(context,
            MaterialPageRoute(builder: (_) => ShelfScreen(api: api)));
        return;
      }
    }

    setState(() {
      _loading = false;
      _error   = '接続できませんでした。URLとトークンを確認してください。';
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF1e1e2e),
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(32),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 440),
            child: _waitingApproval ? _buildWaitingApproval() : _buildLoginForm(),
          ),
        ),
      ),
    );
  }

  Widget _buildWaitingApproval() {
    return Column(
      children: [
        const Icon(Icons.menu_book, size: 72, color: Color(0xFF89b4fa)),
        const SizedBox(height: 12),
        const Text('ComicServer',
            style: TextStyle(
                fontSize: 28,
                fontWeight: FontWeight.bold,
                color: Color(0xFF89b4fa))),
        const SizedBox(height: 48),
        const Icon(Icons.devices, size: 64, color: Color(0xFFA6E3A1)),
        const SizedBox(height: 24),
        const Text('承認待ち',
            style: TextStyle(
                fontSize: 20,
                fontWeight: FontWeight.bold,
                color: Color(0xFFcdd6f4))),
        const SizedBox(height: 16),
        const Text(
          'サーバーの「端末管理」で\nこの端末を承認してください',
          textAlign: TextAlign.center,
          style: TextStyle(color: Color(0xFFa6adc8), fontSize: 15, height: 1.6),
        ),
        const SizedBox(height: 24),
        const CircularProgressIndicator(color: Color(0xFF89b4fa)),
        const SizedBox(height: 20),
        Text('残り約 $_approvalTimeout 秒',
            style: const TextStyle(color: Color(0xFF585b70), fontSize: 13)),
        const SizedBox(height: 32),
        TextButton(
          onPressed: () {
            _approvalTimer?.cancel();
            setState(() {
              _waitingApproval = false;
              _regToken        = '';
              _pendingBaseUrl  = '';
              _approvalTimeout = 180;
              _error           = '';
            });
          },
          child: const Text('キャンセル', style: TextStyle(color: Color(0xFFf38ba8))),
        ),
      ],
    );
  }

  Widget _buildLoginForm() {
    return Column(
      children: [
        const Icon(Icons.menu_book, size: 72, color: Color(0xFF89b4fa)),
        const SizedBox(height: 12),
        const Text('ComicServer',
            style: TextStyle(
                fontSize: 28,
                fontWeight: FontWeight.bold,
                color: Color(0xFF89b4fa))),
        const SizedBox(height: 40),
        _field(_urlCtrl,  'サーバーURL',  Icons.dns,  false),
        const SizedBox(height: 6),
        Align(
          alignment: Alignment.centerRight,
          child: TextButton.icon(
            onPressed: _discovering ? null : _discover,
            icon: _discovering
                ? const SizedBox(
                    width: 16, height: 16,
                    child: CircularProgressIndicator(
                        strokeWidth: 2, color: Color(0xFF89b4fa)))
                : const Icon(Icons.wifi_find,
                    color: Color(0xFF89b4fa), size: 20),
            label: Text(_discovering ? '探索中…' : 'LAN内のサーバーを探す',
                style: const TextStyle(color: Color(0xFF89b4fa))),
          ),
        ),
        const SizedBox(height: 6),
        _field(_tokenCtrl, '認証トークン', Icons.key, true),
        const SizedBox(height: 12),
        _buildTurnSection(),
        const SizedBox(height: 24),
        if (_error.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 16),
            child: Text(_error,
                style: const TextStyle(color: Color(0xFFf38ba8))),
          ),
        SizedBox(
          width: double.infinity,
          height: 50,
          child: ElevatedButton(
            onPressed: _loading ? null : _connect,
            style: ElevatedButton.styleFrom(
              backgroundColor: const Color(0xFF89b4fa),
              foregroundColor: const Color(0xFF1e1e2e),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(10)),
            ),
            child: _loading
                ? const CircularProgressIndicator(
                    color: Color(0xFF1e1e2e), strokeWidth: 2)
                : const Text('接続',
                    style: TextStyle(
                        fontSize: 16, fontWeight: FontWeight.bold)),
          ),
        ),
      ],
    );
  }

  /// TURN設定（折りたたみ・任意）。外出先でSTUNでも繋がらない環境向け。
  /// 標準は空＝STUN-only。入力すればWebRTCのiceServersに追加される。
  Widget _buildTurnSection() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        InkWell(
          onTap: () => setState(() => _showTurn = !_showTurn),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Row(
              children: [
                Icon(_showTurn ? Icons.expand_less : Icons.expand_more,
                    color: const Color(0xFF585b70), size: 20),
                const SizedBox(width: 4),
                const Text('TURNサーバー設定（任意・繋がらない時のみ）',
                    style: TextStyle(color: Color(0xFF585b70), fontSize: 13)),
              ],
            ),
          ),
        ),
        if (_showTurn) ...[
          const SizedBox(height: 8),
          _field(_turnUrlCtrl, 'TURN URL（例 turn:host:3478）', Icons.lan, false),
          const SizedBox(height: 8),
          _field(_turnUserCtrl, 'TURN ユーザー名', Icons.person, false),
          const SizedBox(height: 8),
          _field(_turnCredCtrl, 'TURN 認証情報', Icons.password, true),
          const SizedBox(height: 4),
          const Text('※ 中継の通信費は挿したTURNサーバーの持ち主負担になります',
              style: TextStyle(color: Color(0xFF585b70), fontSize: 11)),
        ],
      ],
    );
  }

  Widget _field(TextEditingController c, String label, IconData icon, bool isPass) {
    return TextField(
      controller: c,
      obscureText: isPass && _obscure,
      style: const TextStyle(color: Color(0xFFcdd6f4)),
      decoration: InputDecoration(
        labelText: label,
        labelStyle: const TextStyle(color: Color(0xFFa6adc8)),
        prefixIcon: Icon(icon, color: const Color(0xFF585b70)),
        suffixIcon: isPass
            ? IconButton(
                icon: Icon(_obscure ? Icons.visibility : Icons.visibility_off,
                    color: const Color(0xFF585b70)),
                onPressed: () => setState(() => _obscure = !_obscure),
              )
            : null,
        filled: true,
        fillColor: const Color(0xFF181825),
        border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(10),
            borderSide: BorderSide.none),
      ),
    );
  }
}
