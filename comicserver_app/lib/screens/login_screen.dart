import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';
import 'shelf_screen.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _urlCtrl  = TextEditingController(text: 'http://192.168.0.25:8765');
  final _userCtrl = TextEditingController(text: 'admin');
  final _passCtrl = TextEditingController();
  bool _loading = false;
  bool _obscure = true;
  String _error = '';

  @override
  void initState() {
    super.initState();
    _loadSaved();
  }

  Future<void> _loadSaved() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _urlCtrl.text  = prefs.getString('url')  ?? _urlCtrl.text;
      _userCtrl.text = prefs.getString('user') ?? _userCtrl.text;
      _passCtrl.text = prefs.getString('pass') ?? '';
    });
  }

  Future<void> _connect() async {
    setState(() { _loading = true; _error = ''; });
    final url  = _urlCtrl.text.trim().replaceAll(RegExp(r'/$'), '');
    final user = _userCtrl.text.trim();
    final pass = _passCtrl.text;

    final api = ApiService(baseUrl: url, username: user, password: pass);
    final ok  = await api.testConnection();

    if (!mounted) return;
    if (ok) {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('url',  url);
      await prefs.setString('user', user);
      await prefs.setString('pass', pass);
      Navigator.pushReplacement(context,
          MaterialPageRoute(builder: (_) => ShelfScreen(api: api)));
    } else {
      setState(() {
        _loading = false;
        _error   = '接続できませんでした。URL・ユーザー名・パスワードを確認してください。';
      });
    }
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
            child: Column(
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
                const SizedBox(height: 14),
                _field(_userCtrl, 'ユーザー名',   Icons.person, false),
                const SizedBox(height: 14),
                _field(_passCtrl, 'パスワード',   Icons.lock,   true),
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
            ),
          ),
        ),
      ),
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
