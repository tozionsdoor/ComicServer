import 'dart:async';
import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/material.dart';
import '../models/book.dart';
import '../services/api_service.dart';
import 'reader_screen.dart';
import 'login_screen.dart';
import 'history_screen.dart';
import '../widgets/cover_image.dart';
import '../widgets/reconnect_banner.dart';
import '../widgets/banner_ad_widget.dart';
import '../services/ads_service.dart';

class ShelfScreen extends StatefulWidget {
  final ApiService api;
  const ShelfScreen({super.key, required this.api});
  @override
  State<ShelfScreen> createState() => _ShelfScreenState();
}

class _ShelfScreenState extends State<ShelfScreen> with WidgetsBindingObserver {
  final List<String> _pathStack = [];
  FolderContents? _contents;
  bool _loading = true;
  String _error  = '';
  final _searchCtrl = TextEditingController();
  bool _searching   = false;
  bool _rescanning  = false;
  bool _opening     = false;   // 本を開く処理の多重実行ガード（広告後の誤オープン対策）
  List<BookItem> _allBooks = [];
  StreamSubscription<List<ConnectivityResult>>? _connectivitySub;

  // フォルダ毎のスクロール位置（パス→オフセット）。戻った時に復元する。
  final ScrollController _gridScroll = ScrollController();
  final Map<String, double> _scrollOffsets = {};

  // カバー画像の取りこぼしは CoverImage が画像ごとに再取得して救う。
  // それでも直らない＝経路が死んでいる疑いがある時だけ、ここで1回 reconnect()
  // を試み、経路が実際に張り替わった場合のみ全表紙を作り直す（_imgGen++）。
  // 生きている（＝張り替わらない）なら作り直さず、無駄なリトライ連鎖を断つ。
  int      _imgGen         = 0;
  bool     _imgRecovering  = false;
  DateTime _lastImgRecover = DateTime.fromMillisecondsSinceEpoch(0);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _load('');
    _loadAllBooks();
    // ネットワーク変化（WiFi→モバイル等）を検知して自動再接続
    _connectivitySub = Connectivity().onConnectivityChanged.listen((results) {
      if (results.any((r) => r != ConnectivityResult.none)) {
        _onResume();
      }
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _connectivitySub?.cancel();
    _searchCtrl.dispose();
    _gridScroll.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // バックグラウンド復帰時だけ経路を検証し、死んでいれば繋ぎ直す（常駐なし）
    if (state == AppLifecycleState.resumed) _onResume();
  }

  Future<void> _onResume() async {
    final before = widget.api.baseUrl;
    final ok = await widget.api.reconnect();
    if (!mounted) return;
    // 経路が変わった（HTTP復帰や新しいloopbackポート）時だけ再読込してURLを張り替え
    if (ok && widget.api.baseUrl != before) {
      _load(_currentPath);
      _loadAllBooks();
    }
  }

  // CoverImage が再試行を使い切っても直らなかった時の保険（経路死の疑い）。
  void _onImageError() {
    if (_imgRecovering) return;
    final now = DateTime.now();
    if (now.difference(_lastImgRecover) < const Duration(seconds: 8)) return;
    _imgRecovering = true;
    _lastImgRecover = now;
    () async {
      try {
        final before = widget.api.baseUrl;
        final ok = await widget.api.reconnect();
        if (!mounted || !ok) return;
        // 経路が実際に張り替わった時だけ全表紙を作り直す。生きていれば何もしない
        // （ミッシングな表紙に対して永久リトライしないため）。
        if (widget.api.baseUrl != before) setState(() => _imgGen++);
      } finally {
        _imgRecovering = false;
      }
    }();
  }

  Future<void> _loadAllBooks() async {
    try {
      // 検索用に全冊リスト（全フォルダ横断）を取得
      final all = await widget.api.getBooks();
      if (mounted) setState(() => _allBooks = all);
    } catch (_) {}
  }

  Future<void> _load(String path) async {
    setState(() { _loading = true; _error = ''; });
    try {
      final c = await widget.api.getFolders(path);
      setState(() { _contents = c; _loading = false; });
      _restoreScroll(path);
    } catch (e) {
      setState(() { _loading = false; _error = e.toString(); });
    }
  }

  // 指定フォルダで保存していたスクロール位置を、再描画後に復元する。
  void _restoreScroll(String path) {
    final target = _scrollOffsets[path];
    if (target == null || target <= 0) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_gridScroll.hasClients) return;
      _gridScroll.jumpTo(target.clamp(0.0, _gridScroll.position.maxScrollExtent));
    });
  }

  Future<void> _rescan() async {
    if (_rescanning) return;
    setState(() => _rescanning = true);
    try {
      final books = await widget.api.rescan();
      await _load(_currentPath);
      await _loadAllBooks();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('再スキャン完了: $books冊')),
      );
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('再スキャン失敗: $e')),
      );
    } finally {
      if (mounted) setState(() => _rescanning = false);
    }
  }

  void _openFolder(String path) {
    if (_gridScroll.hasClients) _scrollOffsets[_currentPath] = _gridScroll.offset;
    _pathStack.add(_contents!.path);
    _load(path);
  }

  void _goBack() {
    if (_pathStack.isEmpty) return;
    if (_gridScroll.hasClients) _scrollOffsets[_currentPath] = _gridScroll.offset;
    final prev = _pathStack.removeLast();
    _load(prev);
  }

  // 履歴から開いた本を閉じた後、その本のフォルダへ移動する。
  // pathの祖先フォルダをすべて_pathStackに積み直し、戻るボタンで通常の
  // 階層ナビゲーションに合流できるようにする。
  void _navigateToFolder(String path) {
    _pathStack.clear();
    if (path.isNotEmpty) {
      final parts = path.split('/').where((p) => p.isNotEmpty).toList();
      String acc = '';
      _pathStack.add(acc);
      for (var i = 0; i < parts.length - 1; i++) {
        acc = acc.isEmpty ? parts[i] : '$acc/${parts[i]}';
        _pathStack.add(acc);
      }
    }
    _load(path);
  }

  Future<void> _openBook(BookItem book, {List<BookItem>? siblings}) async {
    // 多重オープン防止。インタースティシャル広告を閉じた瞬間のタップが下の
    // 本棚へ貫通し、別の巻がもう1冊開いてしまう（広告後に違う巻が開く）不具合の対策。
    // リーダーを開いている間はガードを維持し、戻ってきたら解除する。
    if (_opening) return;
    _opening = true;
    try {
      await AdsService.maybeShowInterstitial();
      if (!mounted) return;
      final books = siblings ?? _contents?.books ?? <BookItem>[];
      final idx   = books.indexWhere((b) => b.id == book.id);
      await Navigator.push(
        context,
        MaterialPageRoute(
          builder: (_) => ReaderScreen(
            api:       widget.api,
            book:      book,
            siblings:  books,
            bookIndex: idx >= 0 ? idx : 0,
          ),
        ),
      );
    } finally {
      _opening = false;
    }
  }

  String get _currentPath => _contents?.path ?? '';
  bool get _canGoBack => _pathStack.isNotEmpty;

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: !_canGoBack,
      onPopInvokedWithResult: (didPop, result) {
        if (!didPop) _goBack();
      },
      child: Scaffold(
        backgroundColor: const Color(0xFF1e1e2e),
        appBar: AppBar(
          backgroundColor: const Color(0xFF181825),
          leading: _canGoBack
              ? IconButton(
                  icon: const Icon(Icons.arrow_back, color: Color(0xFF89b4fa)),
                  onPressed: _goBack)
              : IconButton(
                  icon: const Icon(Icons.logout, color: Color(0xFF585b70)),
                  onPressed: () => Navigator.pushReplacement(context,
                      MaterialPageRoute(builder: (_) => const LoginScreen())),
                ),
          title: _canGoBack
              ? Text(_currentPath.split('/').last,
                  style: const TextStyle(color: Color(0xFFcdd6f4), fontSize: 16))
              : const Text('ArcHive',
                  style: TextStyle(
                      color: Color(0xFF89b4fa), fontWeight: FontWeight.bold)),
          actions: [
            IconButton(
              icon: _rescanning
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(
                        strokeWidth: 2,
                        color: Color(0xFF89b4fa),
                      ),
                    )
                  : const Icon(Icons.refresh, color: Color(0xFF89b4fa)),
              tooltip: '本棚を更新',
              onPressed: _rescanning ? null : _rescan,
            ),
            IconButton(
              icon: const Icon(Icons.history, color: Color(0xFF89b4fa)),
              tooltip: '続き / 履歴',
              onPressed: () {
                Navigator.push(context, MaterialPageRoute(
                    builder: (_) => HistoryScreen(api: widget.api, onOpenFolder: _navigateToFolder)));
              },
            ),
          ],
          bottom: PreferredSize(
            preferredSize: const Size.fromHeight(44),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
              child: TextField(
                controller: _searchCtrl,
                style: const TextStyle(color: Color(0xFFcdd6f4), fontSize: 14),
                decoration: InputDecoration(
                  hintText: 'タイトルで検索...',
                  hintStyle: const TextStyle(color: Color(0xFF585b70)),
                  prefixIcon: const Icon(Icons.search, color: Color(0xFF585b70), size: 20),
                  suffixIcon: _searching
                      ? IconButton(
                          icon: const Icon(Icons.close, color: Color(0xFF585b70), size: 18),
                          onPressed: () {
                            _searchCtrl.clear();
                            setState(() => _searching = false);
                          })
                      : null,
                  filled: true,
                  fillColor: const Color(0xFF313244),
                  contentPadding: const EdgeInsets.symmetric(vertical: 0),
                  border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(8),
                      borderSide: BorderSide.none),
                ),
                onChanged: (v) => setState(() => _searching = v.isNotEmpty),
              ),
            ),
          ),
        ),
        body: Stack(children: [
          _buildBody(),
          ReconnectBanner(api: widget.api),
        ]),
        bottomNavigationBar: const BannerAdWidget(),
      ),
    );
  }

  Widget _buildBody() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator(color: Color(0xFF89b4fa)));
    }
    if (_error.isNotEmpty) {
      return Center(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
        const Icon(Icons.error_outline, color: Color(0xFFf38ba8), size: 48),
        const SizedBox(height: 12),
        Text(_error, style: const TextStyle(color: Color(0xFFf38ba8))),
        const SizedBox(height: 16),
        ElevatedButton(onPressed: () => _load(_currentPath), child: const Text('再試行')),
      ]));
    }
    if (_contents == null) return const SizedBox();

    // 検索中は全フォルダ横断（_allBooks）からタイトル一致を表示する
    if (_searching) {
      final query = _searchCtrl.text.toLowerCase();
      final hits = _allBooks
          .where((b) => b.title.toLowerCase().contains(query))
          .toList();
      if (hits.isEmpty) {
        return const Center(
            child: Text('見つかりません', style: TextStyle(color: Color(0xFF585b70))));
      }
      return GridView.builder(
        padding: const EdgeInsets.all(10),
        gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
            maxCrossAxisExtent: 160,
            crossAxisSpacing: 8,
            mainAxisSpacing: 8,
            childAspectRatio: 0.72),
        itemCount: hits.length,
        itemBuilder: (context, i) => _bookCard(hits[i], siblings: hits),
      );
    }

    final folders = _contents!.folders;
    final books = _contents!.books;

    if (folders.isEmpty && books.isEmpty) {
      return const Center(
          child: Text('見つかりません', style: TextStyle(color: Color(0xFF585b70))));
    }

    return GridView.builder(
      // フォルダ毎のスクロール位置は_scrollOffsets/_restoreScrollで管理する
      controller: _gridScroll,
      padding: const EdgeInsets.all(10),
      gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
          maxCrossAxisExtent: 160,
          crossAxisSpacing: 8,
          mainAxisSpacing: 8,
          childAspectRatio: 0.72),
      itemCount: folders.length + books.length,
      itemBuilder: (context, i) {
        if (i < folders.length) return _folderCard(folders[i]);
        return _bookCard(books[i - folders.length]);
      },
    );
  }

  Widget _folderCard(FolderItem f) {
    return GestureDetector(
      onTap: () => _openFolder(f.path),
      child: Container(
        decoration: BoxDecoration(
            color: const Color(0xFF181825),
            borderRadius: BorderRadius.circular(10)),
        child: Column(
          children: [
            Expanded(
              child: ClipRRect(
                borderRadius: const BorderRadius.vertical(top: Radius.circular(10)),
                child: _folderPreview(f),
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
              child: Text(f.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                      color: Color(0xFFcdd6f4), fontSize: 11)),
            ),
            Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Text('${f.count} 冊',
                  style: const TextStyle(color: Color(0xFF585b70), fontSize: 10)),
            ),
          ],
        ),
      ),
    );
  }

  Widget _folderPreview(FolderItem f) {
    if (f.previewIds.isEmpty) {
      return Container(
          color: const Color(0xFF313244),
          child: const Icon(Icons.folder, color: Color(0xFF585b70), size: 48));
    }
    final ids = f.previewIds.take(4).toList();
    return GridView.count(
      crossAxisCount: 2,
      physics: const NeverScrollableScrollPhysics(),
      padding: EdgeInsets.zero,
      mainAxisSpacing: 1,
      crossAxisSpacing: 1,
      children: ids
          .map((id) => CoverImage(
                key: ValueKey('coverprev_${id}_$_imgGen'),
                imageUrl: widget.api.coverUrl(id),
                headers: widget.api.headers,
                cacheManager: widget.api.cacheManager,
                fit: BoxFit.cover,
                onGaveUp: _onImageError,
                placeholder: Container(color: const Color(0xFF313244)),
                errorWidget: Container(color: const Color(0xFF313244)),
              ))
          .toList(),
    );
  }

  Widget _bookCard(BookItem book, {List<BookItem>? siblings}) {
    return GestureDetector(
      onTap: () => _openBook(book, siblings: siblings),
      child: Container(
        decoration: BoxDecoration(
            color: const Color(0xFF181825),
            borderRadius: BorderRadius.circular(8)),
        child: Column(
          children: [
            Expanded(
              child: ClipRRect(
                borderRadius: const BorderRadius.vertical(top: Radius.circular(8)),
                child: CoverImage(
                  key: ValueKey('cover_${book.id}_$_imgGen'),
                  imageUrl: widget.api.coverUrl(book.id),
                  headers: widget.api.headers,
                  cacheManager: widget.api.cacheManager,
                  fit: BoxFit.cover,
                  width: double.infinity,
                  onGaveUp: _onImageError,
                  placeholder: Container(color: const Color(0xFF313244)),
                  errorWidget: Container(
                      color: const Color(0xFF313244),
                      child: const Icon(Icons.broken_image,
                          color: Color(0xFF585b70))),
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 5),
              child: Text(book.title,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                      color: Color(0xFFa6adc8), fontSize: 10, height: 1.3)),
            ),
          ],
        ),
      ),
    );
  }
}
