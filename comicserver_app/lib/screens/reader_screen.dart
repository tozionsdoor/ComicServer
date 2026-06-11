import 'dart:async';
import 'dart:convert';
import 'dart:math' show min, max;
import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter_cache_manager/flutter_cache_manager.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wakelock_plus/wakelock_plus.dart';
import '../models/book.dart';
import '../services/api_service.dart';
import '../services/ads_service.dart';
import '../widgets/reconnect_banner.dart';


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

class _ReaderScreenState extends State<ReaderScreen> with WidgetsBindingObserver {
  int    _total         = 0;
  int    _page          = 0;      // 現在の manga ページ番号
  bool   _rtl           = true;
  bool   _spread        = false;
  bool   _uiVisible     = false;
  bool   _loading       = true;
  bool   _dialogShowing = false;
  String _error         = '';

  late final PageController _pageCtrl;
  late ScrollController _filmCtrl;

  // アスペクト比キャッシュ
  final Map<int, double> _ratioCache = {};
  double _refRatio = 0.69;   // 代表的な縦長ページ比（幅/高さ）

  BuildContext? _ctx; // プリフェッチ用コンテキスト

  List<_SpreadUnit> _units = [];
  int _spreadPairStart = 1;
  bool _filmUserScrolling = false;
  bool _filmNeedsSync = false;
  int? _pendingFilmIndex;
  double _filmViewportW = 0;

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

  StreamSubscription<List<ConnectivityResult>>? _connectivitySub;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _pageCtrl = PageController();
    _connectivitySub = Connectivity().onConnectivityChanged.listen((results) {
      if (results.any((r) => r != ConnectivityResult.none)) {
        _onResume();
      }
    });
    _filmCtrl = ScrollController();
    _loadPrefs();
    _loadInfo();
    WakelockPlus.enable();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // 復帰時に経路を検証し、死んでいれば繋ぎ直す（常駐なしのオンデマンド再接続）
    if (state == AppLifecycleState.resumed) _onResume();
  }

  Future<void> _onResume() async {
    final before = widget.api.baseUrl;
    final ok = await widget.api.reconnect();
    if (!mounted) return;
    // 経路が変わった時だけ再描画（pageUrlが変わりCachedNetworkImageが再取得する）
    if (ok && widget.api.baseUrl != before) {
      setState(() {});
      _prefetchAround(_pageToUnitIndex(_page));
      _syncFilmToIndex(_currentPvIndex(), animate: false);
    }
  }

  Future<void> _loadPrefs() async {
    final p = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() {
      _rtl    = p.getBool('rtl')    ?? true;
      _spread = p.getBool('spread') ?? false;
      _spreadPairStart = (p.getInt('spread_pair_offset') ?? 1).clamp(0, 1);
      _heightFrac = p.getDouble('height_frac') ?? 0;
    });
    _rebuildUnits();
  }

  Future<void> _savePrefs() async {
    final p = await SharedPreferences.getInstance();
    await p.setBool('rtl',    _rtl);
    await p.setBool('spread', _spread);
    await p.setInt('spread_pair_offset', _spreadPairStart.clamp(0, 1));
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
          if (!mounted) return;
          final target = _total - 1;
          if (_spread) {
            setState(() {
              _unitKeys.clear();
              _rebuildUnits();
            });
          }
          _jumpToMangaPage(target);
          _prefetchAround(_pageToUnitIndex(_page));
          _scheduleFilmSyncToIndex(_currentPvIndex(), animate: false);
        });
      } else {
        // 前回の続きがあればそのページから開く
        final p = await SharedPreferences.getInstance();
        final saved = p.getInt('progress_${widget.book.id}') ?? 0;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted) return;
          if (saved > 0 && saved < _total) {
            if (_spread) {
              setState(() {
                _unitKeys.clear();
                _rebuildUnits();
              });
            }
            _jumpToMangaPage(saved);
            _prefetchAround(_pageToUnitIndex(_page));
            _scheduleFilmSyncToIndex(_currentPvIndex(), animate: false);
          } else {
            _prefetchAround(0);
            _scheduleFilmSyncToIndex(0, animate: false);
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
  static const double _kFilmSlotW = 88.0;
  static const double _kFilmThumbW = 78.0;
  static const double _kFilmThumbH = 118.0;

  final List<int> _prefetchQueue   = [];
  int             _prefetchInFlight = 0;

  // 画像の自己回復:
  // ページ画像(CachedNetworkImage)はAPI経路(_getWithRecovery)を通らないため、
  // 経路が一時的に切れると（IPv6プレフィックス/一時アドレスのローテーション、
  // アイドルでのフロー切断、keep-alive接続の死活など）静的なエラー表示を出すだけで
  // 自動復旧しなかった。失敗を検知したら reconnect() で生きた経路を確保し、
  // 世代カウンタ _imgGen を進めて画像ウィジェットを作り直す（新規接続で取り直す）。
  int      _imgGen         = 0;     // ++ で全 _img / サムネを作り直す
  bool     _imgRecovering  = false; // reconnect の多重起動ガード
  DateTime _lastImgRecover = DateTime.fromMillisecondsSinceEpoch(0); // スロットル基点

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
          cacheManager: widget.api.cacheManager,
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
    WidgetsBinding.instance.removeObserver(this);
    _connectivitySub?.cancel();
    _pageCtrl.dispose();
    _filmCtrl.dispose();
    WakelockPlus.disable();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    super.dispose();
  }

  // ── ユニット構築 ───────────────────────────────────────────────────────────
  void _rebuildUnits() {
    if (_total == 0) { _units = []; return; }

    // 単ページモード: 1ページ=1ユニット
    if (!_spread) {
      _units = [for (int i = 0; i < _total; i++) _SpreadUnit(i)];
      return;
    }

    bool wide(int p) {
      final r = _ratioCache[p];
      if (r == null) return false;
      return r > _refRatio * 1.8;
    }

    final list = <_SpreadUnit>[];
    final offset = _spreadPairStart.clamp(0, 1);
    int i = 0;
    // セグメント先頭（本の先頭・横長ページ直後）。横長ページは見開きペアを
    // リセットするので、「1ページずらす」のオフセットを各セグメント先頭に
    // 適用しないと、横長ページより後ろではペアの偶奇を直せない
    // （横長の表紙を持つ本で「1ページずらす」が効かない不具合の原因）。
    bool segmentStart = true;
    while (i < _total) {
      // 横長（合成見開き）は単独表示し、次ページから新しいセグメントを開始
      if (wide(i)) {
        list.add(_SpreadUnit(i));
        i++;
        segmentStart = true;
        continue;
      }
      // セグメント先頭でオフセット=1なら、先頭1枚を単独にしてペアの偶奇をずらす
      if (segmentStart && offset == 1) {
        segmentStart = false;
        list.add(_SpreadUnit(i));
        i++;
        continue;
      }
      segmentStart = false;
      // 次ページが範囲外/横長ならペアにせず単独表示（横長合成見開きの貼り付き防止）
      if (i + 1 >= _total || wide(i + 1)) {
        list.add(_SpreadUnit(i));
        i++;
      } else {
        list.add(_SpreadUnit(i, i + 1));
        i += 2;
      }
    }
    _units = list;
  }

  int _pageToUnitIndex(int mangaPage) {
    for (int u = 0; u < _units.length; u++) {
      if (_units[u].first == mangaPage) return u;
    }
    for (int u = 0; u < _units.length; u++) {
      if (_units[u].second == mangaPage) return u;
    }
    return 0;
  }

  int get _pvCount => _units.length;
  int _pvToManga(int pvIdx) =>
      (pvIdx >= 0 && pvIdx < _units.length) ? _units[pvIdx].first : 0;

  int _currentPvIndex() {
    if (_pageCtrl.hasClients) {
      final p = _pageCtrl.page;
      if (p != null) return p.round().clamp(0, max(0, _pvCount - 1));
    }
    return _pageToUnitIndex(_page).clamp(0, max(0, _pvCount - 1));
  }

  void _jumpToMangaPage(int mangaPage) {
    final target = mangaPage.clamp(0, _total > 0 ? _total - 1 : 0);
    final pvIdx = _pageToUnitIndex(target);
    final displayPage = _spread ? _pvToManga(pvIdx) : target;
    if (_pageCtrl.hasClients) _pageCtrl.jumpToPage(pvIdx);
    setState(() => _page = displayPage);
    _saveProgress();
  }

  void _syncFilmToIndex(int filmIndex, {bool animate = true}) {
    if (_total <= 0) return;
    final filmCount = _filmItemCount();
    if (filmCount <= 0) return;
    final targetIndex = filmIndex.clamp(0, filmCount - 1);
    _pendingFilmIndex = targetIndex;
    if (!_filmCtrl.hasClients || _filmViewportW <= 0) return;
    final position = _filmCtrl.position;
    // レイアウト未確定（content dimensions未設定）ならpendingのまま再試行に任せる
    if (!position.hasContentDimensions) return;

    final displayIndex = _rtl ? (filmCount - 1 - targetIndex) : targetIndex;
    final slotW = _filmSlotWidth(_filmViewportW);
    final rawOffset =
        displayIndex * slotW - (_filmViewportW - slotW) / 2;
    final maxOffset = position.maxScrollExtent;
    final offset = rawOffset.clamp(0.0, maxOffset);

    if ((_filmCtrl.offset - offset).abs() < 0.5) {
      _pendingFilmIndex = null;
      return;
    }
    if (animate) {
      _filmCtrl.animateTo(
        offset,
        duration: const Duration(milliseconds: 120),
        curve: Curves.easeOut,
      );
    } else {
      _filmCtrl.jumpTo(offset);
    }
    _pendingFilmIndex = null;
  }

  void _scheduleFilmSyncToIndex(int filmIndex, {bool animate = false}) {
    final filmCount = _filmItemCount();
    if (filmCount <= 0) return;
    final target = filmIndex.clamp(0, filmCount - 1);
    _pendingFilmIndex = target;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        _syncFilmToIndex(target, animate: animate);
      });
    });
  }

  int _filmItemCount() => _spread ? _units.length : _total;

  void _resetFilmController() {
    final old = _filmCtrl;
    _filmCtrl = ScrollController();
    _filmViewportW = 0;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      old.dispose();
    });
  }

  double _filmSlotWidth(double viewportW) {
    if (!_spread) return _kFilmSlotW;
    if (viewportW <= 0) return 160.0;
    return (viewportW / 2.45).clamp(148.0, 188.0);
  }

  double _filmThumbWidth(double slotW) {
    if (!_spread) return _kFilmThumbW;
    return max(136.0, slotW - 10.0);
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
      'rel': widget.book.rel,
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
      cacheManager: widget.api.cacheManager,
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
      }, onError: (_, __) {
        // 比率検出の失敗は致命的でない（_refRatioにフォールバック・再ビルドで再試行）。
        // 接続由来の失敗は表示画像の errorWidget → _onImageError が回復を担う。
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
        content: Text('${target.title}\nOpen this volume?',
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
      AdsService.recordVolumeRead();
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
    // 見開きモード: ペアの右ページ(first)から左ページ(second)へは常にページ単位で送る。
    // ・拡大中(画面幅より広い)＝端未達なら左ページへスクロール、端なら次ユニットへ
    // ・画面に収まる見開き＝スクロール余地が無いので、右ページ表示中は_pageだけ
    //   左ページへ進め、次タップで次ユニットへ（=2タップで1見開き=ページ単位送り）
    if (_spread) {
      final pvIdx = _currentPvIndex();
      if (pvIdx < _units.length && _units[pvIdx].isPair) {
        final unit = _units[pvIdx];
        final state = _unitKeys[pvIdx]?.currentState;
        if (state != null) {
          if (state.hasScrollRoom()) {
            if (!state.isAtEnd()) {
              state.animateToEnd();
              setState(() => _page = unit.second!);
              _saveProgress();
              return;
            }
          } else if (_page == unit.first) {
            setState(() => _page = unit.second!);
            _saveProgress();
            return;
          }
        }
      }
    }
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
    // 見開きモード: ペアの左ページ(second)から右ページ(first)へは常にページ単位で戻る。
    // ・拡大中＝先頭未達なら右ページへスクロール、先頭なら前ユニットへ
    // ・画面に収まる見開き＝左ページ表示中は_pageだけ右ページへ戻し、次タップで前ユニットへ
    if (_spread) {
      final pvIdx = _currentPvIndex();
      if (pvIdx < _units.length && _units[pvIdx].isPair) {
        final unit = _units[pvIdx];
        final state = _unitKeys[pvIdx]?.currentState;
        if (state != null) {
          if (state.hasScrollRoom()) {
            if (!state.isAtStart()) {
              state.animateToStart();
              setState(() => _page = unit.first);
              _saveProgress();
              return;
            }
          } else if (_page == unit.second) {
            setState(() => _page = unit.first);
            _saveProgress();
            return;
          }
        }
      }
    }
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
    _filmUserScrolling = false;
    _filmNeedsSync = false;
    _resetFilmController();
    setState(() { _rtl = !_rtl; _unitKeys.clear(); });
    _savePrefs();
    _scheduleFilmSyncToIndex(_currentPvIndex(), animate: false);
  }
  void _toggleUI() {
    final closing = _uiVisible;
    if (closing) {
      _filmUserScrolling = false;
      _filmNeedsSync = false;
      _syncFilmToIndex(_currentPvIndex(), animate: false);
    }
    setState(() => _uiVisible = !_uiVisible);
    if (!closing) {
      _scheduleFilmSyncToIndex(_currentPvIndex(), animate: false);
    }
  }

  void _toggleSpread() {
    final savedManga = _page;
    _filmUserScrolling = false;
    _filmNeedsSync = false;
    _resetFilmController();
    setState(() {
      _spread = !_spread;
      _hUser  = false;  // モード変更で高さをデフォルトに戻す
      _unitKeys.clear();
      if (_lastSize != null) _H = _defaultH(_lastSize!);
      _rebuildUnits();
    });
    _savePrefs();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _jumpToMangaPage(savedManga);
      _prefetchAround(_pageToUnitIndex(_page));
      _scheduleFilmSyncToIndex(_currentPvIndex(), animate: false);
    });
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
              final retreating = _didRetreat;
              _didRetreat = false;
              // 前へ戻った場合、見開きペアなら左ページ(second)から始める
              final newPage = retreating && _spread && pv < _units.length && _units[pv].isPair
                  ? _units[pv].second!
                  : _pvToManga(pv);
              setState(() => _page = newPage);
              _saveProgress();
              if (!_filmUserScrolling) {
                _filmNeedsSync = false;
                _syncFilmToIndex(pv, animate: true);
              } else {
                _filmNeedsSync = true;
              }
              if (retreating) {
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

          ReconnectBanner(api: widget.api),
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
            Expanded(
              child: _MarqueeText(
                text: widget.book.title,
                style: const TextStyle(color: Colors.white, fontSize: 13),
              ),
            ),
            _uiBtn(_rtl ? '右綴じ' : '左綴じ', _toggleRtl, active: _rtl),
            _uiBtn(_spread ? '見開き' : '単ページ', _toggleSpread, active: _spread),
            if (_spread) _uiBtn('1ページずらす', _shiftSpreadByOne),
          ]),
        ),
      ),
    );
  }

  void _shiftSpreadByOne() {
    if (!_spread || _total <= 1) return;
    final currentUnit =
        _units.isEmpty ? _SpreadUnit(_page) : _units[_pageToUnitIndex(_page)];
    final target = (currentUnit.second ?? min(currentUnit.first + 1, _total - 1))
        .clamp(0, _total - 1)
        .toInt();
    _filmUserScrolling = false;
    _filmNeedsSync = false;
    _resetFilmController();
    setState(() {
      _didRetreat = false;
      _unitKeys.clear();
      _spreadPairStart = _spreadPairStart == 0 ? 1 : 0;
      _rebuildUnits();
    });
    _savePrefs();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _jumpToMangaPage(target);
      _prefetchAround(_pageToUnitIndex(_page));
      _scheduleFilmSyncToIndex(_currentPvIndex(), animate: false);
    });
  }

  // ── 下部オーバーレイ ──────────────────────────────────────────────────────
  Widget _volumeNavButton({
    required bool show,
    required bool prev,
    required bool alignRight,
  }) {
    if (!show) return const SizedBox();
    final idx = widget.bookIndex + (prev ? -1 : 1);
    if (idx < 0 || idx >= widget.siblings.length) return const SizedBox();
    final icon = prev
        ? (_rtl ? Icons.skip_next : Icons.skip_previous)
        : (_rtl ? Icons.skip_previous : Icons.skip_next);
    const titleStyle = TextStyle(color: Color(0xFF89b4fa), fontSize: 11);
    return TextButton(
      onPressed: () => _askVolume(prev: prev),
      style: TextButton.styleFrom(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        minimumSize: const Size(0, 36),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.max,
        children: alignRight
            ? [
                Expanded(
                  child: _MarqueeText(
                    text: widget.siblings[idx].title,
                    style: titleStyle,
                    textAlign: TextAlign.right,
                  ),
                ),
                const SizedBox(width: 6),
                Icon(icon, color: const Color(0xFF89b4fa), size: 16),
              ]
            : [
                Icon(icon, color: const Color(0xFF89b4fa), size: 16),
                const SizedBox(width: 6),
                Expanded(
                  child: _MarqueeText(
                    text: widget.siblings[idx].title,
                    style: titleStyle,
                  ),
                ),
              ],
      ),
    );
  }

  Widget _bottomOverlay(BuildContext context) {
    return Positioned(bottom: 0, left: 0, right: 0,
      child: GestureDetector(onTap: _toggleUI,
        child: Container(
          decoration: const BoxDecoration(gradient: LinearGradient(
            begin: Alignment.bottomCenter, end: Alignment.topCenter,
            colors: [Colors.black87, Colors.transparent])),
          padding: EdgeInsets.fromLTRB(
              8, 16, 8, MediaQuery.of(context).padding.bottom + 8),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text('${_page + 1} / $_total',
                style: const TextStyle(color: Colors.white70, fontSize: 13)),
            const SizedBox(height: 4),
            _buildFilmStripV2(),
            const SizedBox(height: 8),
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
              Row(children: [
                // 右綴じは下部ナビの左右を反転: 左=次の巻 / 右=前の巻
                // 左綴じは従来通り: 左=前の巻 / 右=次の巻
                Expanded(
                  child: !_rtl
                      ? _volumeNavButton(
                          show: widget.bookIndex > 0,
                          prev: true,
                          alignRight: false,
                        )
                      : _volumeNavButton(
                          show: widget.bookIndex < widget.siblings.length - 1,
                          prev: false,
                          alignRight: false,
                        ),
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: !_rtl
                      ? _volumeNavButton(
                          show: widget.bookIndex < widget.siblings.length - 1,
                          prev: false,
                          alignRight: true,
                        )
                      : _volumeNavButton(
                          show: widget.bookIndex > 0,
                          prev: true,
                          alignRight: true,
                        ),
                ),
              ]),
          ]),
        ),
      ),
    );
  }

  Rect? _currentContentRect() {
    final pv = _pageCtrl.page?.round() ?? 0;
    return _unitKeys[pv]?.currentState?.contentRect();
  }

  Widget _buildFilmStripV2() {
    if (_total <= 0) return const SizedBox.shrink();
    final filmItems = _spread
        ? _units
        : [for (int i = 0; i < _total; i++) _SpreadUnit(i)];
    final filmCount = filmItems.length;
    if (filmCount <= 0) return const SizedBox.shrink();

    Widget thumbImage(int page, {int memWidth = 220}) {
      return CachedNetworkImage(
        key: ValueKey('thumb_${page}_$_imgGen'),
        imageUrl: widget.api.pageUrl(widget.book.id, page),
        httpHeaders: widget.api.headers,
        cacheManager: widget.api.cacheManager,
        memCacheWidth: memWidth,
        maxWidthDiskCache: memWidth,
        fit: BoxFit.cover,
        fadeInDuration: Duration.zero,
        placeholder: (_, __) => Container(
          color: const Color(0xFF1e1e2e),
        ),
        errorWidget: (_, __, ___) {
          _onImageError();
          return const ColoredBox(
            color: Color(0xFF1e1e2e),
            child: Center(
              child: Icon(Icons.broken_image, color: Colors.white30, size: 16),
            ),
          );
        },
      );
    }

    return SizedBox(
      height: 126,
      child: LayoutBuilder(
        builder: (context, constraints) {
          _filmViewportW = constraints.maxWidth;
          if (_pendingFilmIndex != null) {
            WidgetsBinding.instance.addPostFrameCallback((_) {
              if (!mounted) return;
              final pending = _pendingFilmIndex;
              if (pending != null) _syncFilmToIndex(pending, animate: false);
            });
          }
          final slotW = _filmSlotWidth(_filmViewportW);
          final thumbW = _filmThumbWidth(slotW);
          const thumbH = _kFilmThumbH;
          return NotificationListener<ScrollNotification>(
            onNotification: (n) {
              if (n is ScrollStartNotification && n.dragDetails != null) {
                _filmUserScrolling = true;
              } else if (n is ScrollEndNotification) {
                _filmUserScrolling = false;
                if (_filmNeedsSync) {
                  _filmNeedsSync = false;
                  _syncFilmToIndex(_currentPvIndex(), animate: true);
                }
              } else if (n is UserScrollNotification &&
                  n.direction == ScrollDirection.idle) {
                _filmUserScrolling = false;
                if (_filmNeedsSync) {
                  _filmNeedsSync = false;
                  _syncFilmToIndex(_currentPvIndex(), animate: true);
                }
              }
              return false;
            },
            child: ListView.builder(
                  controller: _filmCtrl,
                  scrollDirection: Axis.horizontal,
                  physics: const BouncingScrollPhysics(),
                  clipBehavior: Clip.none,
                  itemCount: filmCount,
                  itemBuilder: (context, displayIndex) {
                    final filmIndex =
                        _rtl ? (filmCount - 1 - displayIndex) : displayIndex;
                    final item = filmItems[filmIndex];
                    final page = item.first;
                    final selected =
                        item.first == _page || item.second == _page;
                    return SizedBox(
                      width: slotW,
                      child: Center(
                        child: GestureDetector(
                          onTap: () {
                            _filmUserScrolling = false;
                            _filmNeedsSync = false;
                            _jumpToMangaPage(page);
                            _prefetchAround(_pageToUnitIndex(page));
                            _syncFilmToIndex(filmIndex, animate: true);
                          },
                          child: AnimatedContainer(
                            duration: const Duration(milliseconds: 120),
                            curve: Curves.easeOut,
                            width: thumbW,
                            height: thumbH,
                            decoration: BoxDecoration(
                              borderRadius: BorderRadius.circular(6),
                              border: Border.all(
                                color: selected
                                    ? const Color(0xFF89b4fa)
                                    : Colors.white24,
                                width: selected ? 1.8 : 1.0,
                              ),
                            ),
                            clipBehavior: Clip.antiAlias,
                            child: item.isPair
                                ? Row(
                                    children: [
                                      Expanded(
                                        child: thumbImage(
                                          _rtl ? item.second! : item.first,
                                          memWidth: 160,
                                        ),
                                      ),
                                      Expanded(
                                        child: thumbImage(
                                          _rtl ? item.first : item.second!,
                                          memWidth: 160,
                                        ),
                                      ),
                                    ],
                                  )
                                : thumbImage(page),
                          ),
                        ),
                      ),
                    );
                  },
                ),
          );
        },
      ),
    );
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
  // 画像読み込み失敗時の自己回復。8秒スロットル＋多重起動ガードで reconnect を1回に
  // 集約し、生きた経路を確保できたら _imgGen を進めて画像を作り直す（baseUrlが同じでも
  // 新規接続で取り直すため、死んだkeep-alive接続も捨てられる）。本物の404等で恒久的に
  // 失敗する場合もスロットルで暴走しない（reconnectは生存pingで即returnする）。
  void _onImageError() {
    if (_imgRecovering) return;
    final now = DateTime.now();
    if (now.difference(_lastImgRecover) < const Duration(seconds: 8)) return;
    _imgRecovering  = true;
    _lastImgRecover = now;
    () async {
      try {
        final ok = await widget.api.reconnect();
        if (!mounted || !ok) return;
        setState(() => _imgGen++);
        _prefetchAround(_pageToUnitIndex(_page));
      } finally {
        _imgRecovering = false;
      }
    }();
  }

  Widget _img(int n, BoxFit fit) {
    return CachedNetworkImage(
      key:            ValueKey('img_${n}_$_imgGen'),
      imageUrl:       widget.api.pageUrl(widget.book.id, n),
      httpHeaders:    widget.api.headers,
      cacheManager:   widget.api.cacheManager,
      fit:            fit,
      fadeInDuration: Duration.zero,
      placeholder: (_, __) => const Center(
          child: CircularProgressIndicator(color: Color(0xFF89b4fa), strokeWidth: 2)),
      errorWidget: (_, __, ___) {
        _onImageError();
        return const Center(
            child: Icon(Icons.broken_image, color: Colors.white30, size: 40));
      },
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

class _MarqueeText extends StatefulWidget {
  final String text;
  final TextStyle style;
  final TextAlign textAlign;

  const _MarqueeText({
    required this.text,
    required this.style,
    this.textAlign = TextAlign.left,
  });

  @override
  State<_MarqueeText> createState() => _MarqueeTextState();
}

class _MarqueeTextState extends State<_MarqueeText>
    with SingleTickerProviderStateMixin {
  static const double _gap = 24.0;
  static const double _speedPxPerSec = 25.0; // さらに少し速め
  static const int _pauseEndMs = 700;        // 末端で一時停止
  late final AnimationController _ctrl;
  double _overflow = 0;
  double _boxWidth = 0;
  double _textWidth = 0;
  double _moveFraction = 1.0;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(vsync: this);
  }

  @override
  void didUpdateWidget(covariant _MarqueeText oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.text != widget.text || oldWidget.style != widget.style) {
      _scheduleRecalc();
    }
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  void _scheduleRecalc() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || _boxWidth <= 0) return;
      _recalc(_boxWidth);
    });
  }

  void _startTicker() {
    final travel = _textWidth + _gap;
    final moveMs = (travel / _speedPxPerSec * 1000).round().clamp(5000, 24000);
    final totalMs = moveMs + _pauseEndMs;
    _moveFraction = moveMs / totalMs;
    _ctrl.stop();
    _ctrl.duration = Duration(milliseconds: totalMs);
    _ctrl.value = 0;
    _ctrl.repeat();
  }

  void _recalc(double maxWidth) {
    final painter = TextPainter(
      text: TextSpan(text: widget.text, style: widget.style),
      textDirection: TextDirection.ltr,
      maxLines: 1,
    )..layout(minWidth: 0, maxWidth: double.infinity);

    _textWidth = painter.width;
    final newOverflow = _textWidth - maxWidth;
    final overflow = newOverflow > 0 ? newOverflow : 0.0;
    final changed = (overflow - _overflow).abs() >= 0.5;
    if (!changed) {
      if (overflow > 0 && !_ctrl.isAnimating) {
        _startTicker();
        if (mounted) setState(() {});
      }
      return;
    }

    final hadOverflow = _overflow > 0;
    _overflow = overflow;
    if (_overflow <= 0) {
      _ctrl.stop();
      if (mounted) setState(() {});
      return;
    }

    _startTicker();
    if (!hadOverflow && mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (_, constraints) {
      final w = constraints.maxWidth;
      if ((w - _boxWidth).abs() > 0.5) {
        _boxWidth = w;
        _scheduleRecalc();
      }

      if (_overflow <= 0) {
        return Text(
          widget.text,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          textAlign: widget.textAlign,
          style: widget.style,
        );
      }

      return ClipRect(
        child: AnimatedBuilder(
          animation: _ctrl,
          builder: (_, __) {
            final t = _ctrl.value;
            final travel = _textWidth + _gap;
            final moveT = t <= _moveFraction ? (t / _moveFraction) : 1.0;
            final dx = -travel * moveT;
            return Transform.translate(
              offset: Offset(dx, 0),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    widget.text,
                    maxLines: 1,
                    softWrap: false,
                    overflow: TextOverflow.visible,
                    textAlign: widget.textAlign,
                    style: widget.style,
                  ),
                  const SizedBox(width: _gap),
                  Text(
                    widget.text,
                    maxLines: 1,
                    softWrap: false,
                    overflow: TextOverflow.visible,
                    textAlign: widget.textAlign,
                    style: widget.style,
                  ),
                ],
              ),
            );
          },
        ),
      );
    });
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

  // 見開きが画面幅より広く、横スクロールの余地があるか（=拡大表示中か）。
  // 余地がなければ見開き全体が画面に収まっており、ページ送りは_pageの更新で行う。
  bool hasScrollRoom() {
    if (!_c.hasClients) return false;
    return _c.position.maxScrollExtent > 1;
  }

  // タップ1ページ送り用: スクロール端判定＆アニメーション移動
  bool isAtEnd() {
    if (!_c.hasClients) return true;
    final max = _c.position.maxScrollExtent;
    return max <= 0 || _c.position.pixels >= max - 1;
  }
  bool isAtStart() {
    if (!_c.hasClients) return true;
    final max = _c.position.maxScrollExtent;
    return max <= 0 || _c.position.pixels <= 1;
  }
  void animateToEnd() {
    if (_c.hasClients) _c.animateTo(_c.position.maxScrollExtent,
        duration: const Duration(milliseconds: 220), curve: Curves.easeOut);
  }
  void animateToStart() {
    if (_c.hasClients) _c.animateTo(0,
        duration: const Duration(milliseconds: 220), curve: Curves.easeOut);
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
