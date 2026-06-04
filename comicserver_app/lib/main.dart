import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'screens/login_screen.dart';
import 'screens/shelf_screen.dart';
import 'services/api_service.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
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
  @override
  void initState() {
    super.initState();
    _autoLogin();
  }

  Future<void> _autoLogin() async {
    final prefs = await SharedPreferences.getInstance();
    final url   = prefs.getString('url');
    final token = prefs.getString('token');
    final ipv6  = prefs.getString('ipv6');

    if (url != null && token != null && token.isNotEmpty) {
      // 候補（LAN直 / IPv6）を同時に試し、最初に応答したもので接続
      final candidates =
          buildCandidates(primaryUrl: url, ipv6: ipv6);
      final working = await ApiService.resolveBaseUrl(candidates, token);
      if (!mounted) return;
      if (working != null) {
        final api = ApiService(
            baseUrl: working, token: token,
            candidates: candidates);
        // 次回のため最新の ipv6 を保存（fire-and-forget）
        api.getConnectionInfo().then((info) async {
          if (info == null) return;
          final v6 = (info['ipv6'] ?? '').toString();
          if (v6.isNotEmpty) await prefs.setString('ipv6', v6);
        });
        Navigator.pushReplacement(context,
            MaterialPageRoute(builder: (_) => ShelfScreen(api: api)));
        return;
      }
    }
    if (!mounted) return;
    Navigator.pushReplacement(context,
        MaterialPageRoute(builder: (_) => const LoginScreen()));
  }

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      backgroundColor: Color(0xFF1e1e2e),
      body: Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Icon(Icons.menu_book, size: 72, color: Color(0xFF89b4fa)),
          SizedBox(height: 16),
          CircularProgressIndicator(color: Color(0xFF89b4fa)),
        ]),
      ),
    );
  }
}
