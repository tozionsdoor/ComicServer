import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'firebase_options.dart';
import 'screens/login_screen.dart';
import 'screens/shelf_screen.dart';
import 'services/api_service.dart';
import 'services/webrtc_service.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    await Firebase.initializeApp(options: DefaultFirebaseOptions.currentPlatform);
  } catch (_) {
    // Firebase未設定の場合はWebRTC P2Pが無効になるが、LAN/VPN動作は継続する
  }
  runApp(const ComicServerApp());
}

class ComicServerApp extends StatelessWidget {
  const ComicServerApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ComicServer',
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
    _autoLogin();
  }

  Future<void> _autoLogin() async {
    final prefs  = await SharedPreferences.getInstance();
    final url    = prefs.getString('url');
    final token  = prefs.getString('token');
    final ipv6   = prefs.getString('ipv6');
    final roomId = prefs.getString('room_id') ?? '';
    final turnUrl  = prefs.getString('turn_url');
    final turnUser = prefs.getString('turn_username');
    final turnCred = prefs.getString('turn_credential');

    final certFingerprint = prefs.getString('cert_fingerprint') ?? '';

    if (url != null && token != null && token.isNotEmpty) {
      _setStatus('保存した接続先を確認中…');
      final candidates = buildCandidates(primaryUrl: url, ipv6: ipv6);
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
