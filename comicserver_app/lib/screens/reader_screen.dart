import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wakelock_plus/wakelock_plus.dart';
import '../models/book.dart';
import '../services/api_service.dart';

class ReaderScreen extends StatefulWidget {
  final ApiService api;
  final BookItem book;
  const ReaderScreen({super.key, required this.api, required this.book});
  @override
  State<ReaderScreen> createState() => _ReaderScreenState();
}

class _ReaderScreenState extends State<ReaderScreen> {
  int _total    = 0;
  int _page     = 0;
  bool _rtl     = true;   // 右綴じ（RTL）
  bool _spread  = false;  // 見開き
  bool _uiVisible = false;
  bool _loading   = true;

  late PageController _pageCtrl;

  @override
  void initState() {
    super.initState();
    _loadPrefs();
    _loadInfo();
    WakelockPlus.enable();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
  }

  Future<void> _loadPrefs() async {
    final p = await SharedPreferences.getInstance();
    setState(() {
      _rtl    = p.getBool('rtl')    ?? true;
      _spread = p.getBool('spread') ?? false;
    });
  }

  Future<void> _savePrefs() async {
    final p = await SharedPreferences.getInstance();
    await p.setBool('rtl',    _rtl);
    await p.setBool('spread', _spread);
  }

  Future<void> _loadInfo() async {
    try {
      final info = await widget.api.getBookInfo(widget.book.id);
      setState(() { _total = info.count; _loading = false; });
      _pageCtrl = PageController(initialPage: 0);
    } catch (e) {
      setState(() => _loading = false);
    }
  }

  @override
  void dispose() {
    WakelockPlus.disable();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    if (!_loading) _pageCtrl.dispose();
    super.dispose();
  }

  int get _step => _spread ? 2 : 1;

  // PageView のページ index → 実際のページ番号
  int _pageViewToBook(int pvIdx) => pvIdx * _step;
  int get _pvTotal => _total == 0 ? 0 : (_total / _step).ceil();

  void _toggleUI() => setState(() => _uiVisible = !_uiVisible);

  void _toggleRtl() {
    setState(() => _rtl = !_rtl);
    _savePrefs();
  }

  void _toggleSpread() {
    setState(() => _spread = !_spread);
    _savePrefs();
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(
        backgroundColor: Colors.black,
        body: Center(child: CircularProgressIndicator(color: Color(0xFF89b4fa))),
      );
    }
    return Scaffold(
      backgroundColor: Colors.black,
      body: Stack(
        children: [
          // ── ページビュー ──────────────────────────────────────────
          GestureDetector(
            onTap: _toggleUI,
            child: PageView.builder(
              controller: _pageCtrl,
              reverse: _rtl,           // RTLは右から左へ
              itemCount: _pvTotal,
              onPageChanged: (idx) =>
                  setState(() => _page = _pageViewToBook(idx)),
              itemBuilder: (context, pvIdx) {
                final n = _pageViewToBook(pvIdx);
                if (_spread && n + 1 < _total) {
                  // 見開き：2枚を横並び（RTLは右が先）
                  return Row(
                    children: _rtl
                        ? [_pageImg(n + 1), _pageImg(n)]
                        : [_pageImg(n),     _pageImg(n + 1)],
                  );
                }
                return _pageImg(n);
              },
            ),
          ),

          // ── UI オーバーレイ ────────────────────────────────────────
          if (_uiVisible) ...[
            // 上部 (本棚に戻る + タイトル + 設定)
            Positioned(
              top: 0, left: 0, right: 0,
              child: Container(
                decoration: const BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                    colors: [Colors.black87, Colors.transparent],
                  ),
                ),
                padding: EdgeInsets.fromLTRB(
                    8, MediaQuery.of(context).padding.top + 4, 8, 16),
                child: Row(
                  children: [
                    IconButton(
                      icon: const Icon(Icons.arrow_back, color: Colors.white),
                      onPressed: () => Navigator.pop(context),
                    ),
                    Expanded(
                      child: Text(widget.book.title,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                              color: Colors.white, fontSize: 13)),
                    ),
                    _uiBtn(_rtl ? 'RTL' : 'LTR', _toggleRtl,
                        active: _rtl),
                    _uiBtn(_spread ? '見開き' : '単ページ', _toggleSpread,
                        active: _spread),
                  ],
                ),
              ),
            ),
            // 下部（ページスライダー）
            Positioned(
              bottom: 0, left: 0, right: 0,
              child: Container(
                decoration: const BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.bottomCenter,
                    end: Alignment.topCenter,
                    colors: [Colors.black87, Colors.transparent],
                  ),
                ),
                padding: EdgeInsets.fromLTRB(
                    16, 16, 16, MediaQuery.of(context).padding.bottom + 8),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('${_page + 1} / $_total',
                        style: const TextStyle(
                            color: Colors.white70, fontSize: 13)),
                    const SizedBox(height: 6),
                    SliderTheme(
                      data: SliderTheme.of(context).copyWith(
                        activeTrackColor: const Color(0xFF89b4fa),
                        thumbColor: const Color(0xFF89b4fa),
                        inactiveTrackColor: Colors.white24,
                        overlayColor: Colors.transparent,
                        trackHeight: 3,
                      ),
                      child: Slider(
                        value: _page.toDouble(),
                        min: 0,
                        max: (_total - 1).toDouble(),
                        divisions: _total > 1 ? _total - 1 : 1,
                        onChanged: (v) {
                          final n = v.round();
                          _goToPage(n);
                        },
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }

  void _goToPage(int n) {
    final clamped = n.clamp(0, _total - 1);
    setState(() => _page = clamped);
    final pvIdx = (clamped / _step).floor();
    _pageCtrl.jumpToPage(pvIdx);
  }

  Widget _pageImg(int n) {
    return Expanded(
      child: CachedNetworkImage(
        imageUrl: widget.api.pageUrl(widget.book.id, n),
        httpHeaders: widget.api.headers,
        fit: BoxFit.contain,
        fadeInDuration: Duration.zero,
        placeholder: (_, __) => const Center(
            child: CircularProgressIndicator(
                color: Color(0xFF89b4fa), strokeWidth: 2)),
        errorWidget: (_, __, ___) => const Center(
            child: Icon(Icons.broken_image, color: Colors.white30, size: 48)),
      ),
    );
  }

  Widget _uiBtn(String label, VoidCallback onTap, {bool active = false}) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 3),
      child: GestureDetector(
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
          decoration: BoxDecoration(
            color: active
                ? const Color(0xFF89b4fa).withOpacity(0.25)
                : Colors.black54,
            border: Border.all(
                color: active
                    ? const Color(0xFF89b4fa)
                    : Colors.white30),
            borderRadius: BorderRadius.circular(6),
          ),
          child: Text(label,
              style: TextStyle(
                  color: active
                      ? const Color(0xFF89b4fa)
                      : Colors.white70,
                  fontSize: 12)),
        ),
      ),
    );
  }
}
