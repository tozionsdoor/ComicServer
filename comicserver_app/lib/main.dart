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
    final url  = prefs.getString('url');
    final user = prefs.getString('user');
    final pass = prefs.getString('pass');

    if (!mounted) return;

    if (url != null && user != null && pass != null) {
      final api = ApiService(baseUrl: url, username: user, password: pass);
      final ok  = await api.testConnection();
      if (!mounted) return;
      if (ok) {
        Navigator.pushReplacement(context,
            MaterialPageRoute(builder: (_) => ShelfScreen(api: api)));
        return;
      }
    }
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
