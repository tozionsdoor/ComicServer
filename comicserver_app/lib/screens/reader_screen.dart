import 'dart:convert';
import 'dart:math' show min, max;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter_cache_manager/flutter_cache_manager.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wakelock_plus/wakelock_plus.dart';
import '../models/book.dart';
import '../services/api_service.dart';

// 漫画ページ用の共有キャッシュ。既定（200ファイル/30日）では読み返しで溢れるため
// 上限を拡大し、表示・先読み・比率検出すべてで同じキャッシュを共有する。
final CacheManager _pageCacheManager = CacheManager(
  Config(
    'comicPageCache',
    stalePeriod: const Duration(days: 90),
    maxNrOfCacheObjects: 2000,
  ),
);

// ─── 見開き1ユニット ──────────────────────────────────────────────────────────
// 1ページ（単独）か 2ページ（見開きペア）か
class _SpreadUnit {
  final int first;
  final int? second;   // null = single（単ページ / 横長合成 / 最終ページ単独）
  const _SpreadUnit(this.first, [this.second]);
  bool get isPair => second != null;
}

class ReaderScreen extends StatefulWidget {
  final ApiService api;
  final BookItem book;
  final List<BookItem> siblings;
  final int bookIndex;
  final bool startFromEnd;

  const ReaderScreen({
    super.key,
    required this.api,
    required this.book,
    this.siblings     = const [],
    this.bookIndex    = 0,
    this.startFromEnd = false,
  });

  @override
  State<ReaderScreen> createState() => _ReaderScreenState();
}

class _ReaderScreenState extends State<ReaderScreen> {
  int    _total         = 0;
  int    _page          = 0;      // 現在の manga ページ番号
  bool   _rtl           = true;
  bool   _spread        = false;
  bool   _uiVisible     = false;
  bool   _loading       = true;
  bool   _dialogShowing = false;
  String _error         = '';

  late final PageController _pageCtrl;

  // アスペクト比キャッシュ
  final Map<int, double> _ratioCache = {};
  double _refRatio = 0.69;   // 代表的な縦長ページ比（幅/高さ）

  BuildContext? _ctx; // プリフェッチ用コンテキスト

  // 見開きユニット
  List<_SpreadUnit> _units = [];
  int _spreadStartPage = 0;

  // 各見開きの状態にアクセスするキー（戻る時に末尾へジャンプするため）
  final Map<int, GlobalKey<_ScrollUnitState>> _unitKeys = {};
  bool _didRetreat = false;   // 直前の操作が「前へ戻る」だったか

  GlobalKey<_ScrollUnitState> _keyFor(int pv) =>
      _unitKeys.putIfAbsent(pv, () => GlobalKey<_ScrollUnitState>());

  // ── スケール固定（固定高さ） ───────────────────────────────────────────────
  double _H        = 0;       // 全ユニット共通の表示高さ(px)。0=未設定
  bool   _hUser    = false;   // ユーザーがピンチで設定したか
  Size?  _lastSize;

  // ピンチ検出
  final Map<int, Offset> _ptrs = {};
  double? _pinchStartDist;
  double  _pinchStartH = 0;
  double  _heightFrac  = 0;    // 表示高さ÷画面高さ（端末内に記憶。0=未設定で初回フィット）

  // 虫眼鏡
  Offset? _magnifierPos;
  static const double _magnifierScale = 2.0;

  @override
  void initState() {
    super.initState();
    _pageCtrl = PageController();
    _loadPrefs();
    _loadInfo();
    WakelockPlus.enable();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
  }

  Future<void> _loadPrefs() async {
    final p = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() {
      _rtl    = p.getBool('rtl')    ?? true;
      _spread = p.getBool('spread') ?? false;
      _heightFrac = p.getDouble('height_frac') ?? 0;
    });
    _rebuildUnits();
  }

  Future<void> _savePrefs() async {
    final p = await SharedPreferences.getInstance();
    await p.setBool('rtl',    _rtl);
    await p.setBool('spread', _spread);
    await p.setDouble('height_frac', _heightFrac);
  }

  Future<void> _loadInfo() async {
    try {
      final info = await widget.api.getBookInfo(widget.book.id);
      if (!mounted) return;
      setState(() { _total = info.count; _loading = false; });
      _rebuildUnits();
      if (widget.startFromEnd && _total > 0) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          _jumpToMangaPage(_total - 1);
          _prefetchAround(_pageToUnitIndex(_total - 1));
        });
      } else {
        // 前回の続きがあればそのページから開く
        final p = await SharedPreferences.getInstance();
        final saved = p.getInt('progress_${widget.book.id}') ?? 0;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted) return;
          if (saved > 0 && saved < _total) {
            _jumpToMangaPage(saved);
            _prefetchAround(_pageToUnitIndex(saved));
          } else {
            _prefetchAround(0);
          }
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() { _loading = false; _error = '読み込み失敗: $e'; });
    }
  }

  // ── プリフェッチ ──────────────────────────────────────────────────────────
  // 現在ユニットの前方 _kPrefetchAhead・後方 _kPrefetchBehind ユニットを先読みする。
  // サーバーを叩きすぎないよう同時実行を _kPrefetchConcurrency 本に制限し、
  // 近いページから順に取得する（早送り耐性を上げつつ現在ページを優先）。
  static const int _kPrefetchAhead       = 8;
  static const int _kPrefetchBehind      = 1;
  static const int _kPrefetchConcurrency = 3;

  final List<int> _prefetchQueue   = [];
  int             _prefetchInFlight = 0;

  void _prefetchAround(int pvIdx) {
    final ctx = _ctx;
    if (ctx == null || !mounted || _units.isEmpty) return;

    // 近い順（前方優先 → 後方）にページ番号を集める
    final pages = <int>[];
    void addUnit(int idx) {
      if (idx < 0 || idx >= _units.length) return;
      final unit = _units[idx];
      if (unit.first < _total) pages.add(unit.first);
      if (unit.second != null && unit.second! < _total) pages.add(unit.second!);
    }
    for (int d = 1; d <= _kPrefetchAhead; d++) {
      addUnit(pvIdx + d);
    }
    for (int d = 1; d <= _kPrefetchBehind; d++) {
      addUnit(pvIdx - d);
    }

    // 現在地が変わったらキューを作り直す（追い越された古い要求は捨てる）
    _prefetchQueue
      ..clear()
      ..addAll(pages.where((n) => n >= 0));
    _pumpPrefetch(ctx);
  }

  void _pumpPrefetch(BuildContext ctx) {
    while (_prefetchInFlight < _kPrefetchConcurrency && _prefetchQueue.isNotEmpty) {
      final n = _prefetchQueue.removeAt(0);
      _prefetchInFlight++;
      precacheImage(
        CachedNetworkImageProvider(
          widget.api.pageUrl(widget.book.id, n),
          headers: widget.api.headers,
          cacheManager: _pageCacheManager,
        ),
        ctx,
        onError: (_, __) {},   // 先読み失敗はログを汚さず無視
      ).whenComplete(() {
        _prefetchInFlight--;
        if (mounted) _pumpPrefetch(ctx);
      });
    }
  }

  @override
  void dispose() {
    _pageCtrl.dispose();
    WakelockPlus.disable();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    super.dispose();
  }

  // ── ユニット構築 ───────────────────────────────────────────────────────────
  void _rebuildUnits({int? startPage}) {
    if (startPage != null) _spreadStartPage = startPage;
    if (_total == 0) { _units = []; return; }

    // 単ページモード: 1ページ=1ユニット
    if (!_spread) {
      _units = [for (int i = 0; i < _total; i++) _SpreadUnit(i)];
      return;
    }

    // 見開きモード: startPage を右側(first)に揃えてペアを組む
    final sp = _spreadStartPage;
    final list = <_SpreadUnit>[];
    int i = 0;
    // 縦長ページ比の1.5倍以上 → 2ページ合成済みの横長と判断し単独表示すべきページ
    bool wide(int p) => (_ratioCache[p] ?? 0) > _refRatio * 1.5;
    // i を単独にすべきか: 自分が横長 / 末尾 / 次ページが横長(=表紙の次の見開き等)ならペアにしない
    void addFrom(int end) {
      while (i < end) {
        if (wide(i) || i + 1 >= end || wide(i + 1)) { list.add(_SpreadUnit(i)); i++; }
        else { list.add(_SpreadUnit(i, i + 1)); i += 2; }
      }
    }
    addFrom(sp);
    addFrom(_total);
    _units = list;
  }

  int _pageToUnitIndex(int mangaPage) {
    for (int u = 0; u < _units.length; u++) {
      if (_units[u].first == mangaPage || _units[u].second == mangaPage) return u;
    }
    return 0;
  }

  int get _pvCount => _units.length;
  int _pvToManga(int pvIdx) =>
      (pvIdx >= 0 && pvIdx < _units.length) ? _units[pvIdx].first : 0;

  void _jumpToMangaPage(int mangaPage) {
    final pvIdx = _pageToUnitIndex(mangaPage);
    if (_pageCtrl.hasClients) _pageCtrl.jumpToPage(pvIdx);
    setState(() => _page = mangaPage);
    _saveProgress();
  }

  // 続きから読むための進捗（現在ページ）と既読履歴を端末内に保存する
  Future<void> _saveProgress() async {
    if (_total <= 0) return;
    final p = await SharedPreferences.getInstance();
    await p.setInt('progress_${widget.book.id}', _page);
    final list = p.getStringList('history') ?? [];
    list.removeWhere((e) {
      try { return (jsonDecode(e) as Map)['id'] == widget.book.id; }
      catch (_) { return false; }
    });
    list.insert(0, jsonEncode({
      'id': widget.book.id, 'title': widget.book.title,
      'page': _page, 'total': _total,
      'ts': DateTime.now().millisecondsSinceEpoch,
    }));
    if (list.length > 100) list.removeRange(100, list.length);
    await p.setStringList('history', list);
  }

  // ── アスペクト比検出 ──────────────────────────────────────────────────────
  void _detectRatio(int n) {
    if (n < 0 || n >= _total || _ratioCache.containsKey(n)) return;
    final provider = CachedNetworkImageProvider(
      widget.api.pageUrl(widget.book.id, n),
      headers: widget.api.headers,
      cacheManager: _pageCacheManager,
    );
    provider.resolve(ImageConfiguration.empty).addListener(
      ImageStreamListener((info, _) {
        if (!mounted) return;
        final r = info.image.width / info.image.height;
        if (!_ratioCache.containsKey(n)) {
          setState(() {
            _ratioCache[n] = r;
            if (r < 1.0 && (_refRatio - 0.69).abs() < 0.001) {
              _refRatio = r;
              if (!_hUser && _lastSize != null) _H = _defaultH(_lastSize!);
            }
            _rebuildUnits();
          });
        }
      }),
    );
  }

  // ── 固定高さの計算 ────────────────────────────────────────────────────────
  // デフォルト: ユニット全体が画面に収まる高さ（縦長は画面幅基準、見開きは2枚ぶん）
  double _computeDefaultH(Size s) {
    final unitRatio = _spread ? (_refRatio * 2) : _refRatio;
    return min(s.width / unitRatio, s.height);
  }

  // 記憶した「画面高さに対する割合」で表示高さを再現（画像比率に依存せず一定の見え方）。
  // 未設定(_heightFrac==0)のときだけ、ユニットが画面に収まる初回フィットを使う。
  double _defaultH(Size s) => _heightFrac > 0
      ? (s.height * _heightFrac).clamp(_minH(), _maxH())
      : _computeDefaultH(s);

  double _minH() => (_lastSize?.height ?? 600) * 0.25;
  double _maxH() => (_lastSize?.height ?? 600);   // 横スクロールのみ対応(=fit height上限)

  double _unitRatio(_SpreadUnit u) {
    if (!u.isPair) return _ratioCache[u.first] ?? _refRatio;
    final lp = _rtl ? u.second! : u.first;
    final rp = _rtl ? u.first   : u.second!;
    return (_ratioCache[lp] ?? _refRatio) + (_ratioCache[rp] ?? _refRatio);
  }

  // ── 巻ナビ ──────────────────────────────────────────────────────────────
  Future<void> _askVolume({required bool prev}) async {
    if (_dialogShowing) return;
    final idx = widget.bookIndex + (prev ? -1 : 1);
    if (idx < 0 || idx >= widget.siblings.length) return;
    _dialogShowing = true;
    final target = widget.siblings[idx];
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF181825),
        title: Text(prev ? '前の巻' : '次の巻',
            style: const TextStyle(color: Color(0xFFcdd6f4))),
        content: Text('「${target.title}」\nに移動しますか？',
            style: const TextStyle(color: Color(0xFFa6adc8))),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false),
              child: const Text('キャンセル',
                  style: TextStyle(color: Color(0xFF585b70)))),
          TextButton(onPressed: () => Navigator.pop(ctx, true),
              child: Text(prev ? '前の巻' : '次の巻',
                  style: const TextStyle(color: Color(0xFF89b4fa)))),
        ],
      ),
    );
    _dialogShowing = false;
    if (ok == true && mounted) {
      Navigator.pushReplacement(context, MaterialPageRoute(
        builder: (_) => ReaderScreen(
          api: widget.api, book: target,
          siblings: widget.siblings, bookIndex: idx,
          startFromEnd: prev,
        ),
      ));
    }
  }

  // ── ユニット送り（タップ＆端スワイプ共通） ──────────────────────────────────
  void _advance() {
    final pv = _pageCtrl.page?.round() ?? 0;
    if (pv < _pvCount - 1) {
      _didRetreat = false;   // 次へ: 新ページは先頭(読み始め側)から
      _pageCtrl.nextPage(
          duration: const Duration(milliseconds: 220), curve: Curves.easeOut);
    } else {
      _askVolume(prev: false);
    }
  }

  void _retreat() {
    final pv = _pageCtrl.page?.round() ?? 0;
    if (pv > 0) {
      _didRetreat = true;    // 前へ: 戻った見開きは末尾(読み終わり側=左ページ)から
      _pageCtrl.previousPage(
          duration: const Duration(milliseconds: 220), curve: Curves.easeOut);
    } else {
      _askVolume(prev: true);
    }
  }

  // ── モード切替 ───────────────────────────────────────────────────────────
  void _toggleRtl() {
    setState(() { _rtl = !_rtl; _unitKeys.clear(); });
    _savePrefs();
  }
  void _toggleUI()  => setState(() => _uiVisible = !_uiVisible);

  void _toggleSpread() {
    final savedManga = _page;
    setState(() {
      _spread = !_spread;
      _hUser  = false;  // モード変更で高さをデフォルトに戻す
      _unitKeys.clear();
      if (_lastSize != null) _H = _defaultH(_lastSize!);
      _rebuildUnits(startPage: savedManga);
    });
    _savePrefs();
    WidgetsBinding.instance.addPostFrameCallback((_) => _jumpToMangaPage(savedManga));
  }

  // ── ピンチ（固定高さ変更） ─────────────────────────────────────────────────
  void _onPtrDown(PointerDownEvent e) { _ptrs[e.pointer] = e.position; }
  void _onPtrUp(int p) {
    _ptrs.remove(p);
    if (_ptrs.length < 2) {
      // ピンチ終了 → 表示高さを「画面高さに対する割合」で記憶（画像が変わっても同じ絶対サイズ）
      if (_pinchStartDist != null && _hUser &&
          _lastSize != null && _lastSize!.height > 0) {
        _heightFrac = (_H / _lastSize!.height).clamp(0.25, 1.0);
        _savePrefs();
      }
      _pinchStartDist = null;
    }
  }
  void _onPtrMove(PointerMoveEvent e) {
    if (_ptrs.containsKey(e.pointer)) _ptrs[e.pointer] = e.position;
    if (_ptrs.length >= 2) {
      final pts = _ptrs.values.toList();
      final d = (pts[0] - pts[1]).distance;
      if (_pinchStartDist == null) {
        _pinchStartDist = d;
        _pinchStartH = _H;
        _hUser = true;
      } else if (_pinchStartDist! > 0) {
        final nh = (_pinchStartH * d / _pinchStartDist!).clamp(_minH(), _maxH());
        if ((nh - _H).abs() > 0.5) setState(() => _H = nh);
      }
    }
  }

  // ── ビルド ──────────────────────────────────────────────────────────────
  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(backgroundColor: Colors.black,
          body: Center(child: CircularProgressIndicator(color: Color(0xFF89b4fa))));
    }
    if (_error.isNotEmpty) {
      return Scaffold(backgroundColor: Colors.black,
          body: Center(child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.error_outline, color: Color(0xFFf38ba8), size: 48),
            const SizedBox(height: 12),
            Text(_error, style: const TextStyle(color: Color(0xFFf38ba8)),
                textAlign: TextAlign.center),
            const SizedBox(height: 20),
            Row(mainAxisSize: MainAxisSize.min, children: [
              ElevatedButton(
                  onPressed: () {
                    setState(() { _loading = true; _error = ''; });
                    _loadInfo();   // 繋ぎ直して再読み込み（ApiServiceが候補を再レース）
                  },
                  child: const Text('再試行')),
              const SizedBox(width: 12),
              TextButton(onPressed: () => Navigator.pop(context),
                  child: const Text('戻る')),
            ]),
          ])));
    }
    if (_total == 0) {
      return Scaffold(backgroundColor: Colors.black,
          appBar: AppBar(backgroundColor: Colors.black,
              leading: IconButton(
                  icon: const Icon(Icons.arrow_back, color: Colors.white),
                  onPressed: () => Navigator.pop(context))),
          body: const Center(child: Text('ページが見つかりませんでした',
              style: TextStyle(color: Colors.white54))));
    }

    _ctx = context;

    // 画面サイズ確定 → デフォルト高さ計算
    final size = MediaQuery.of(context).size;
    if (_lastSize != size) {
      _lastSize = size;
      if (!_hUser) _H = _defaultH(size);
    }
    if (_H == 0) _H = _defaultH(size);

    return Scaffold(
      backgroundColor: Colors.black,
      body: Listener(
        onPointerDown:   _onPtrDown,
        onPointerMove:   _onPtrMove,
        onPointerUp:     (e) => _onPtrUp(e.pointer),
        onPointerCancel: (e) => _onPtrUp(e.pointer),
        child: Stack(children: [
          PageView.builder(
            controller: _pageCtrl,
            reverse:    _rtl,
            physics:    const NeverScrollableScrollPhysics(), // 送りはプログラム制御
            itemCount:  _pvCount,
            onPageChanged: (pv) {
              setState(() => _page = _pvToManga(pv));
              _saveProgress();
              // 「前へ戻る」だった場合、戻った見開きを末尾へジャンプ（左ページ表示）
              if (_didRetreat) {
                _didRetreat = false;
                WidgetsBinding.instance.addPostFrameCallback((_) {
                  _unitKeys[pv]?.currentState?.jumpToEnd();
                });
              }
              _prefetchAround(pv);
            },
            itemBuilder: (_, pv) => _buildUnit(pv, size),
          ),

          // 中央タップ: UI トグル / 長押し: 虫眼鏡
          Positioned.fill(
            child: GestureDetector(
              behavior: HitTestBehavior.translucent,
              onTap: _toggleUI,
              onLongPressStart:      (d) => setState(() => _magnifierPos = d.globalPosition),
              onLongPressMoveUpdate: (d) => setState(() => _magnifierPos = d.globalPosition),
              onLongPressEnd:        (_) => setState(() => _magnifierPos = null),
              onLongPressCancel:     ()  => setState(() => _magnifierPos = null),
            ),
          ),

          if (!_uiVisible) _tapZones(size),

          if (_uiVisible) ...[
            _topOverlay(context),
            _bottomOverlay(context),
          ],

          if (_magnifierPos != null)
            _buildMagnifier(_magnifierPos!, size),
        ]),
      ),
    );
  }

  // ── ユニット描画 ──────────────────────────────────────────────────────────
  Widget _buildUnit(int pvIdx, Size size) {
    if (pvIdx >= _units.length) {
      return SizedBox.expand(child: _img(pvIdx, BoxFit.contain));
    }
    final unit = _units[pvIdx];
    _detectRatio(unit.first);
    if (unit.second != null) _detectRatio(unit.second!);

    final ur = _unitRatio(unit);
    final wc = _H * ur;

    return _ScrollUnit(
      key:          _keyFor(pvIdx),
      contentWidth: wc,
      height:       _H,
      screenW:      size.width,
      screenH:      size.height,
      rtl:          _rtl,
      onAdvance:    _advance,
      onRetreat:    _retreat,
      content:      _unitContent(unit),
    );
  }

  Widget _unitContent(_SpreadUnit u) {
    if (!u.isPair) return _img(u.first, BoxFit.fill);
    final lp = _rtl ? u.second! : u.first;
    final rp = _rtl ? u.first   : u.second!;
    final rL = _ratioCache[lp] ?? _refRatio;
    final rR = _ratioCache[rp] ?? _refRatio;
    return Row(children: [
      Expanded(flex: (rL * 1000).round(), child: _img(lp, BoxFit.fill)),
      Expanded(flex: (rR * 1000).round(), child: _img(rp, BoxFit.fill)),
    ]);
  }

  // ── タップゾーン ─────────────────────────────────────────────────────────
  Widget _tapZones(Size size) {
    final w = size.width;
    return Stack(children: [
      Positioned(left: 0, top: 0, bottom: 0, width: w * 0.25,
        child: GestureDetector(
          behavior: HitTestBehavior.translucent,
          onTap: _rtl ? _advance : _retreat,
        )),
      Positioned(right: 0, top: 0, bottom: 0, width: w * 0.25,
        child: GestureDetector(
          behavior: HitTestBehavior.translucent,
          onTap: _rtl ? _retreat : _advance,
        )),
    ]);
  }

  // ── 上部オーバーレイ ──────────────────────────────────────────────────────
  Widget _topOverlay(BuildContext context) {
    return Positioned(top: 0, left: 0, right: 0,
      child: GestureDetector(onTap: _toggleUI,
        child: Container(
          decoration: const BoxDecoration(gradient: LinearGradient(
            begin: Alignment.topCenter, end: Alignment.bottomCenter,
            colors: [Colors.black87, Colors.transparent])),
          padding: EdgeInsets.fromLTRB(
              4, MediaQuery.of(context).padding.top + 4, 4, 16),
          child: Row(children: [
            IconButton(
              icon: const Icon(Icons.arrow_back, color: Colors.white),
              onPressed: () => Navigator.pop(context)),
            Expanded(child: Text(widget.book.title,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: Colors.white, fontSize: 13))),
            _uiBtn(_rtl ? 'RTL' : 'LTR', _toggleRtl, active: _rtl),
            _uiBtn(_spread ? '見開き' : '単ページ', _toggleSpread, active: _spread),
          ]),
        ),
      ),
    );
  }

  // ── 下部オーバーレイ ──────────────────────────────────────────────────────
  Widget _bottomOverlay(BuildContext context) {
    return Positioned(bottom: 0, left: 0, right: 0,
      child: GestureDetector(onTap: _toggleUI,
        child: Container(
          decoration: const BoxDecoration(gradient: LinearGradient(
            begin: Alignment.bottomCenter, end: Alignment.topCenter,
            colors: [Colors.black87, Colors.transparent])),
          padding: EdgeInsets.fromLTRB(
              16, 16, 16, MediaQuery.of(context).padding.bottom + 8),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text('${_page + 1} / $_total',
                style: const TextStyle(color: Colors.white70, fontSize: 13)),
            const SizedBox(height: 4),
            SliderTheme(
              data: SliderTheme.of(context).copyWith(
                activeTrackColor:   const Color(0xFF89b4fa),
                thumbColor:         const Color(0xFF89b4fa),
                inactiveTrackColor: Colors.white24,
                overlayColor:       Colors.transparent,
                trackHeight: 3),
              child: Slider(
                value: (_rtl ? (_total - 1 - _page) : _page)
                    .toDouble().clamp(0, (_total - 1).toDouble()),
                min: 0, max: (_total - 1).toDouble(),
                divisions: _total > 1 ? _total - 1 : 1,
                onChanged: (v) {
                  final raw = v.round();
                  _jumpToMangaPage(_rtl ? (_total - 1 - raw) : raw);
                },
              ),
            ),
            if (widget.siblings.length > 1)
              Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
                if (widget.bookIndex > 0)
                  TextButton.icon(
                    onPressed: () => _askVolume(prev: true),
                    icon: const Icon(Icons.skip_previous,
                        color: Color(0xFF89b4fa), size: 16),
                    label: Text(widget.siblings[widget.bookIndex - 1].title,
                        style: const TextStyle(color: Color(0xFF89b4fa), fontSize: 11),
                        overflow: TextOverflow.ellipsis))
                else const SizedBox(),
                if (widget.bookIndex < widget.siblings.length - 1)
                  TextButton.icon(
                    onPressed: () => _askVolume(prev: false),
                    icon: const Icon(Icons.skip_next,
                        color: Color(0xFF89b4fa), size: 16),
                    label: Text(widget.siblings[widget.bookIndex + 1].title,
                        style: const TextStyle(color: Color(0xFF89b4fa), fontSize: 11),
                        overflow: TextOverflow.ellipsis))
                else const SizedBox(),
              ]),
          ]),
        ),
      ),
    );
  }

  // ── 虫眼鏡 ────────────────────────────────────────────────────────────────
  Rect? _currentContentRect() {
    final pv = _pageCtrl.page?.round() ?? 0;
    return _unitKeys[pv]?.currentState?.contentRect();
  }

  Widget _buildMagnifier(Offset fingerPos, Size screenSize) {
    final contentRect = _currentContentRect();
    if (contentRect == null) return const SizedBox();

    final magW  = min(390.0, screenSize.width);
    final magH  = min(300.0, screenSize.height);
    final scale = _magnifierScale;

    final ix = (fingerPos.dx - contentRect.left).clamp(0.0, contentRect.width);
    final iy = (fingerPos.dy - contentRect.top).clamp(0.0, contentRect.height);

    double magLeft = fingerPos.dx - magW / 2;
    double magTop  = fingerPos.dy - magH - 40;
    magLeft = magLeft.clamp(0.0, screenSize.width  - magW);
    magTop  = magTop .clamp(0.0, screenSize.height - magH);

    final tx = magW / 2 - ix * scale;
    final ty = magH / 2 - iy * scale;

    final pv   = _pageCtrl.page?.round() ?? 0;
    final unit = pv < _units.length ? _units[pv] : _SpreadUnit(_page);

    return Positioned(
      left: magLeft,
      top:  magTop,
      child: Container(
        width:        magW,
        height:       magH,
        clipBehavior: Clip.antiAlias,
        decoration: BoxDecoration(
          border:       Border.all(color: Colors.white54, width: 1.5),
          borderRadius: BorderRadius.circular(6),
          boxShadow: const [
            BoxShadow(color: Colors.black54, blurRadius: 10, spreadRadius: 2),
          ],
        ),
        child: Stack(
          clipBehavior: Clip.hardEdge,
          children: [
            Positioned(
              left: tx,
              top:  ty,
              child: SizedBox(
                width:  contentRect.width  * scale,
                height: contentRect.height * scale,
                child:  _unitContent(unit),
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ── 画像 ──────────────────────────────────────────────────────────────────
  Widget _img(int n, BoxFit fit) {
    return CachedNetworkImage(
      imageUrl:       widget.api.pageUrl(widget.book.id, n),
      httpHeaders:    widget.api.headers,
      cacheManager:   _pageCacheManager,
      fit:            fit,
      fadeInDuration: Duration.zero,
      placeholder: (_, __) => const Center(
          child: CircularProgressIndicator(color: Color(0xFF89b4fa), strokeWidth: 2)),
      errorWidget: (_, __, ___) => const Center(
          child: Icon(Icons.broken_image, color: Colors.white30, size: 40)),
    );
  }

  // ── UI ボタン ─────────────────────────────────────────────────────────────
  Widget _uiBtn(String label, VoidCallback onTap, {bool active = false}) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 3),
      child: GestureDetector(onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
          decoration: BoxDecoration(
            color: active ? const Color(0xFF89b4fa).withValues(alpha: 0.25)
                          : Colors.black54,
            border: Border.all(
                color: active ? const Color(0xFF89b4fa) : Colors.white30),
            borderRadius: BorderRadius.circular(6)),
          child: Text(label, style: TextStyle(
              color: active ? const Color(0xFF89b4fa) : Colors.white70,
              fontSize: 12)),
        ),
      ),
    );
  }
}

// ─── 見開き1つ分の横スクロールビュー ───────────────────────────────────────────
// ・固定高さ height で content（幅 contentWidth）を表示
// ・画面より広ければ自由横スクロール（任意位置で止まる）
// ・端でさらにドラッグ → onAdvance / onRetreat（次/前の見開きへ）
class _ScrollUnit extends StatefulWidget {
  final Widget content;
  final double contentWidth;
  final double height;
  final double screenW;
  final double screenH;
  final bool   rtl;
  final VoidCallback onAdvance;
  final VoidCallback onRetreat;

  const _ScrollUnit({
    super.key,
    required this.content,
    required this.contentWidth,
    required this.height,
    required this.screenW,
    required this.screenH,
    required this.rtl,
    required this.onAdvance,
    required this.onRetreat,
  });

  @override
  State<_ScrollUnit> createState() => _ScrollUnitState();
}

class _ScrollUnitState extends State<_ScrollUnit> {
  final ScrollController _c = ScrollController();
  double _acc = 0;       // オーバースクロール累積
  bool   _fired = false; // 1ジェスチャーで1回だけ発火
  static const _thresh = 70.0;

  final GlobalKey _contentKey = GlobalKey();

  @override
  void dispose() { _c.dispose(); super.dispose(); }

  // 末尾（読み終わり側）へ即ジャンプ。前の見開きに戻った時に左ページを表示する用
  void jumpToEnd() {
    if (_c.hasClients) _c.jumpTo(_c.position.maxScrollExtent);
  }

  // 虫眼鏡用: コンテンツのグローバルRect
  Rect? contentRect() {
    final box = _contentKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null || !box.attached) return null;
    final pos = box.localToGlobal(Offset.zero);
    return pos & box.size;
  }

  bool _onNotify(ScrollNotification n) {
    if (n is ScrollStartNotification) {
      _acc = 0; _fired = false;
    } else if (n is OverscrollNotification && n.metrics.axis == Axis.horizontal) {
      _acc += n.overscroll;
      if (!_fired) {
        if (_acc > _thresh)  { _fired = true; widget.onAdvance(); }
        if (_acc < -_thresh) { _fired = true; widget.onRetreat(); }
      }
    }
    return false;
  }

  @override
  Widget build(BuildContext context) {
    // 横方向の見かけ幅（画面より狭ければ画面幅にして中央寄せ＝余白でも端スワイプ可）
    final childW = max(widget.contentWidth, widget.screenW);
    return NotificationListener<ScrollNotification>(
      onNotification: _onNotify,
      child: SingleChildScrollView(
        controller: _c,
        scrollDirection: Axis.horizontal,
        reverse: widget.rtl,                 // RTLは右端から読み始め
        // AlwaysScrollable: コンテンツが画面に収まる時もドラッグを受け付け、
        // 端のオーバースクロールでページ送りできるようにする
        physics: const AlwaysScrollableScrollPhysics(
            parent: ClampingScrollPhysics()),
        child: SizedBox(
          width:  childW,
          height: widget.screenH,
          child: Center(
            child: SizedBox(
              key:    _contentKey,
              width:  widget.contentWidth,
              height: widget.height,
              child:  widget.content,
            ),
          ),
        ),
      ),
    );
  }
}
