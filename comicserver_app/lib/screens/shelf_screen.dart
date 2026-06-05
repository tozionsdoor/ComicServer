import 'package:flutter/material.dart';
import 'package:cached_network_image/cached_network_image.dart';
import '../models/book.dart';
import '../services/api_service.dart';
import 'reader_screen.dart';
import 'login_screen.dart';
import 'history_screen.dart';

class ShelfScreen extends StatefulWidget {
  final ApiService api;
  const ShelfScreen({super.key, required this.api});
  @override
  State<ShelfScreen> createState() => _ShelfScreenState();
}

class _ShelfScreenState extends State<ShelfScreen> {
  final List<String> _pathStack = [];
  FolderContents? _contents;
  bool _loading = true;
  String _error  = '';
  final _searchCtrl = TextEditingController();
  bool _searching   = false;
  List<BookItem> _allBooks = [];

  @override
  void initState() {
    super.initState();
    _load('');
    _loadAllBooks();
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
    } catch (e) {
      setState(() { _loading = false; _error = e.toString(); });
    }
  }

  void _openFolder(String path) {
    _pathStack.add(_contents!.path);
    _load(path);
  }

  void _goBack() {
    if (_pathStack.isEmpty) return;
    final prev = _pathStack.removeLast();
    _load(prev);
  }

  void _openBook(BookItem book, {List<BookItem>? siblings}) {
    final books = siblings ?? _contents?.books ?? <BookItem>[];
    final idx   = books.indexWhere((b) => b.id == book.id);
    Navigator.push(
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
  }

  String get _currentPath => _contents?.path ?? '';
  bool get _canGoBack => _pathStack.isNotEmpty;

  @override
  Widget build(BuildContext context) {
    return WillPopScope(
      onWillPop: () async {
        if (_canGoBack) { _goBack(); return false; }
        return true;
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
              : const Text('ComicServer',
                  style: TextStyle(
                      color: Color(0xFF89b4fa), fontWeight: FontWeight.bold)),
          actions: [
            IconButton(
              icon: const Icon(Icons.history, color: Color(0xFF89b4fa)),
              tooltip: '続き / 履歴',
              onPressed: () => Navigator.push(context, MaterialPageRoute(
                  builder: (_) => HistoryScreen(api: widget.api))),
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
        body: _buildBody(),
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
          .map((id) => CachedNetworkImage(
                imageUrl: widget.api.coverUrl(id),
                httpHeaders: widget.api.headers,
                fit: BoxFit.cover,
                placeholder: (_, __) =>
                    Container(color: const Color(0xFF313244)),
                errorWidget: (_, __, ___) =>
                    Container(color: const Color(0xFF313244)),
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
                child: CachedNetworkImage(
                  imageUrl: widget.api.coverUrl(book.id),
                  httpHeaders: widget.api.headers,
                  fit: BoxFit.cover,
                  width: double.infinity,
                  placeholder: (_, __) =>
                      Container(color: const Color(0xFF313244)),
                  errorWidget: (_, __, ___) => Container(
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
