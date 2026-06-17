import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'firebase_options.dart';
import 'screens/login_screen.dart';
import 'screens/shelf_screen.dart';
import 'services/ads_service.dart';
import 'services/api_service.dart';
import 'services/firebase_signaling.dart';
import 'services/webrtc_service.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    await Firebase.initializeApp(options: DefaultFirebaseOptions.currentPlatform);
  } catch (_) {
    // Firebase未設定の場合はWebRTC P2Pが無効になるが、LAN/VPN動作は継続する
  }
  try {
    await AdsService.initialize();
  } catch (_) {
    // 広告SDK初期化失敗時もアプリ本体の動作は継続する
  }
  runApp(const ArcHiveApp());
}

class ArcHiveApp extends StatelessWidget {
  const ArcHiveApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ArcHive',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF89b4fa),
          brightness: Brightness.dark,
        ),
        scaffoldBackgroundColor: const Color(0xFF1e1e2e),
        fontFamily: 'Roboto',
      ),
      home: const _StartScreen(),
    );
  }
}

class _StartScreen extends StatefulWidget {
  const _StartScreen();
  @override
  State<_StartScreen> createState() => _StartScreenState();
}

class _StartScreenState extends State<_StartScreen> {
  String _status = '接続先を確認中…';   // スプラッシュに出す現在の段階

  @override
  void initState() {
    super.initState();
    AdsService.requestConsent();
    _autoLogin();
  }

  Future<void> _autoLogin() async {
    final prefs  = await SharedPreferences.getInstance();
    final url    = prefs.getString('url');
    final token  = prefs.getString('token');
    var ipv6   = prefs.getString('ipv6');
    var ipv4Global = prefs.getString('ipv4_global');
    var ipv4Port   = prefs.getInt('ipv4_port') ?? 0;
    final roomId = prefs.getString('room_id') ?? '';
    final turnUrl  = prefs.getString('turn_url');
    final turnUser = prefs.getString('turn_username');
    final turnCred = prefs.getString('turn_credential');

    final certFingerprint = prefs.getString('cert_fingerprint') ?? '';

    if (url != null && token != null && token.isNotEmpty) {
      _setStatus('保存した接続先を確認中…');
      // 初回/外出先で prefs に直結情報が無いときは、Firebaseの最新接続先で補完する。
      // これでWebRTCに落ちる前にIPv6/IPv4直結（サーバーがUPnPで開けた外部ポート）を試せる。
      if (roomId.isNotEmpty && (ipv4Port == 0 || ipv6 == null || ipv6.isEmpty)) {
        final host = await FirebaseSignaling.readServerHost(roomId);
        if (host != null) {
          final fv6 = (host['ipv6'] ?? '').toString();
          final fv4 = (host['ipv4'] ?? '').toString();
          final fp4 = (host['ipv4_port'] as num?)?.toInt() ?? 0;
          if (fv6.isNotEmpty) { ipv6 = fv6; await prefs.setString('ipv6', fv6); }
          if (fv4.isNotEmpty) { ipv4Global = fv4; await prefs.setString('ipv4_global', fv4); }
          if (fp4 > 0) { ipv4Port = fp4; await prefs.setInt('ipv4_port', fp4); }
        }
      }
      final candidates = buildCandidates(
          primaryUrl: url, ipv6: ipv6, ipv4Global: ipv4Global, ipv4Port: ipv4Port);
      final working = await ApiService.resolveBaseUrl(candidates, token,
          certFingerprint: certFingerprint);
      if (!mounted) return;
      if (working != null) {
        final api = ApiService(
            baseUrl: working, token: token, candidates: candidates,
            roomId: roomId, turnUrl: turnUrl,
            turnUsername: turnUser, turnCredential: turnCred,
            certFingerprint: certFingerprint);
        api.getConnectionInfo().then((info) async {
          if (info == null) return;
          final v6 = (info['ipv6'] ?? '').toString();
          if (v6.isNotEmpty) await prefs.setString('ipv6', v6);
          final g4 = (info['ipv4_global'] ?? '').toString();
          if (g4.isNotEmpty) await prefs.setString('ipv4_global', g4);
          final p4 = (info['ipv4_port'] as num?)?.toInt() ?? 0;
          if (p4 > 0) await prefs.setInt('ipv4_port', p4);
        });
        await _finish('接続しました', () => ShelfScreen(api: api));
        return;
      }

      // HTTP候補が全滅 → WebRTC P2P を試みる
      if (roomId.isNotEmpty) {
        _setStatus('WebRTC(P2P)で接続中…');
        final webRtc = WebRtcService.instance;
        final localUrl = await webRtc.connect(
          roomId: roomId,
          authToken: token,
          turnUrl:        turnUrl,
          turnUsername:   turnUser,
          turnCredential: turnCred,
        );
        if (!mounted) return;
        if (localUrl != null) {
          final api = ApiService(
              baseUrl: localUrl, token: token, candidates: candidates,
              viaWebRtc: true, roomId: roomId, turnUrl: turnUrl,
              turnUsername: turnUser, turnCredential: turnCred,
              certFingerprint: certFingerprint);
          api.getConnectionInfo().then((info) async {
            if (info == null) return;
            final v6 = (info['ipv6'] ?? '').toString();
            if (v6.isNotEmpty) await prefs.setString('ipv6', v6);
            final g4 = (info['ipv4_global'] ?? '').toString();
            if (g4.isNotEmpty) await prefs.setString('ipv4_global', g4);
            final p4 = (info['ipv4_port'] as num?)?.toInt() ?? 0;
            if (p4 > 0) await prefs.setInt('ipv4_port', p4);
          });
          await _finish('P2Pで接続しました', () => ShelfScreen(api: api));
          return;
        }
      }

      // 保存情報はあるが全滅 → 失敗ステータスを少し長めに見せてからログインへ
      if (!mounted) return;
      await _finish('接続できませんでした', () => const LoginScreen(), hold: true);
      return;
    }

    // 保存情報なし（初回起動など）→ そのままログイン画面へ
    if (!mounted) return;
    Navigator.pushReplacement(
        context, MaterialPageRoute(builder: (_) => const LoginScreen()));
  }

  void _setStatus(String s) {
    if (mounted) setState(() => _status = s);
  }

  /// 最終ステータスを少し長め（成功は短め・失敗は長め）に見せてから遷移する。
  Future<void> _finish(String msg, Widget Function() page,
      {bool hold = false}) async {
    _setStatus(msg);
    await Future.delayed(Duration(milliseconds: hold ? 1300 : 650));
    if (!mounted) return;
    Navigator.pushReplacement(
        context, MaterialPageRoute(builder: (_) => page()));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF1e1e2e),
      body: Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const Icon(Icons.menu_book, size: 72, color: Color(0xFF89b4fa)),
          const SizedBox(height: 16),
          const CircularProgressIndicator(color: Color(0xFF89b4fa)),
          const SizedBox(height: 22),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: Text(_status,
                textAlign: TextAlign.center,
                style: const TextStyle(color: Color(0xFFa6adc8), fontSize: 14)),
          ),
        ]),
      ),
    );
  }
}
