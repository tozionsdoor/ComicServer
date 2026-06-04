import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';
import '../services/discovery_service.dart';
import 'shelf_screen.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _urlCtrl   = TextEditingController(text: 'http://192.168.0.25:8765');
  final _tokenCtrl = TextEditingController();
  bool _loading = false;
  bool _discovering = false;
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
      _urlCtrl.text   = prefs.getString('url')   ?? _urlCtrl.text;
      _tokenCtrl.text = prefs.getString('token') ?? '';
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
      setState(() {
        _urlCtrl.text   = selected.baseUrl;
        _tokenCtrl.text = selected.token;   // LANペアリングで受け取ったトークン
      });
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('ipv6', selected.ipv6);       // 外出先用IPv6
    }
  }

  // 接続後、最新の ipv6 を保存しておく
  void _refreshConnInfo(ApiService api) {
    api.getConnectionInfo().then((info) async {
      if (info == null) return;
      final pr = await SharedPreferences.getInstance();
      final v6 = (info['ipv6'] ?? '').toString();
      if (v6.isNotEmpty) await pr.setString('ipv6', v6);
    });
  }

  Future<void> _connect() async {
    setState(() { _loading = true; _error = ''; });
    final url   = _urlCtrl.text.trim().replaceAll(RegExp(r'/$'), '');
    final token = _tokenCtrl.text.trim();

    final prefs  = await SharedPreferences.getInstance();
    final ipv6   = prefs.getString('ipv6');     // 発見時に保存された外出先用IPv6
    final candidates =
        buildCandidates(primaryUrl: url, ipv6: ipv6);
    final working = await ApiService.resolveBaseUrl(candidates, token);

    if (!mounted) return;
    if (working != null) {
      await prefs.setString('url',   url);     // 家のLANアドレスを正として保存
      await prefs.setString('token', token);
      final api = ApiService(
          baseUrl: working, token: token, candidates: candidates);
      _refreshConnInfo(api);
      Navigator.pushReplacement(context,
          MaterialPageRoute(builder: (_) => ShelfScreen(api: api)));
    } else {
      setState(() {
        _loading = false;
        _error   = '接続できませんでした。URLとトークンを確認してください。';
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
