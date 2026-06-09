import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../models/book.dart';
import '../services/api_service.dart';
import 'reader_screen.dart';

/// 読書履歴（続きから / 既読一覧）。端末内 SharedPreferences の 'history' を表示する。
class HistoryScreen extends StatefulWidget {
  final ApiService api;
  const HistoryScreen({super.key, required this.api});

  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen> {
  List<Map<String, dynamic>> _items = [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final p = await SharedPreferences.getInstance();
    final raw = p.getStringList('history') ?? [];
    final items = <Map<String, dynamic>>[];
    for (final e in raw) {
      try {
        items.add(jsonDecode(e) as Map<String, dynamic>);
      } catch (_) {}
    }
    if (!mounted) return;
    setState(() {
      _items = items;
      _loading = false;
    });
  }

  Future<void> _clear() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: const Color(0xFF181825),
        title: const Text('履歴を消去', style: TextStyle(color: Color(0xFFcdd6f4))),
        content: const Text('読書履歴をすべて消去しますか？',
            style: TextStyle(color: Color(0xFFa6adc8))),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('キャンセル')),
          TextButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('消去',
                  style: TextStyle(color: Color(0xFFf38ba8)))),
        ],
      ),
    );
    if (ok == true) {
      final p = await SharedPreferences.getInstance();
      await p.remove('history');
      _load();
    }
  }

  // 履歴の個別削除（カード右上の×）。該当本の進捗(progress_<id>)も消す。
  Future<void> _delete(String id) async {
    final p = await SharedPreferences.getInstance();
    final raw = p.getStringList('history') ?? [];
    raw.removeWhere((e) {
      try {
        return (jsonDecode(e) as Map)['id'] == id;
      } catch (_) {
        return false;
      }
    });
    await p.setStringList('history', raw);
    await p.remove('progress_$id');
    _load();
  }

  Future<void> _open(Map<String, dynamic> it) async {
    final book = BookItem(
        id: it['id'] as String,
        title: (it['title'] as String?) ?? '',
        rel: (it['rel'] as String?) ?? '');

    // 同じフォルダの兄弟巻リストを取得して「前の巻/次の巻」ナビを有効化する
    List<BookItem> siblings = [book];
    int bookIndex = 0;
    final rel = book.rel;
    if (rel.isNotEmpty) {
      try {
        final parentPath = rel.contains('/')
            ? rel.substring(0, rel.lastIndexOf('/'))
            : '';
        final contents = await widget.api.getFolders(parentPath);
        final idx = contents.books.indexWhere((b) => b.id == book.id);
        if (idx >= 0) {
          siblings = contents.books;
          bookIndex = idx;
        }
      } catch (_) {
        // API失敗時は単巻のまま開く
      }
    }
    if (!mounted) return;

    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => ReaderScreen(
            api: widget.api, book: book, siblings: siblings, bookIndex: bookIndex),
      ),
    ).then((_) => _load()); // 戻ったら進捗を反映して並び替え
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF1e1e2e),
      appBar: AppBar(
        backgroundColor: const Color(0xFF181825),
        iconTheme: const IconThemeData(color: Color(0xFF89b4fa)),
        title: const Text('続き / 履歴',
            style: TextStyle(
                color: Color(0xFF89b4fa), fontWeight: FontWeight.bold)),
        actions: [
          if (_items.isNotEmpty)
            IconButton(
                icon: const Icon(Icons.delete_outline, color: Color(0xFFf38ba8)),
                tooltip: '履歴を消去',
                onPressed: _clear),
        ],
      ),
      body: _loading
          ? const Center(
              child: CircularProgressIndicator(color: Color(0xFF89b4fa)))
          : _items.isEmpty
              ? const Center(
                  child: Text('まだ読書履歴がありません',
                      style: TextStyle(color: Color(0xFF585b70))))
              : GridView.builder(
                  padding: const EdgeInsets.all(10),
                  gridDelegate:
                      const SliverGridDelegateWithMaxCrossAxisExtent(
                          maxCrossAxisExtent: 160,
                          crossAxisSpacing: 8,
                          mainAxisSpacing: 8,
                          childAspectRatio: 0.62),
                  itemCount: _items.length,
                  itemBuilder: (context, i) => _card(_items[i]),
                ),
    );
  }

  Widget _card(Map<String, dynamic> it) {
    final id = it['id'] as String;
    final page = (it['page'] as num?)?.toInt() ?? 0;
    final total = (it['total'] as num?)?.toInt() ?? 0;
    final pct = total > 0 ? ((page + 1) / total * 100).round() : 0;
    return GestureDetector(
      onTap: () => _open(it),
      child: Stack(
        children: [
          Container(
            decoration: BoxDecoration(
                color: const Color(0xFF181825),
                borderRadius: BorderRadius.circular(8)),
            child: Column(
              children: [
                Expanded(
                  child: ClipRRect(
                    borderRadius:
                        const BorderRadius.vertical(top: Radius.circular(8)),
                    child: CachedNetworkImage(
                      imageUrl: widget.api.coverUrl(id),
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
                  padding:
                      const EdgeInsets.symmetric(horizontal: 5, vertical: 4),
                  child: Text((it['title'] as String?) ?? '',
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          color: Color(0xFFa6adc8), fontSize: 10, height: 1.3)),
                ),
                Padding(
                  padding: const EdgeInsets.only(bottom: 5),
                  child: Text('${page + 1}/$total・$pct%',
                      style: const TextStyle(
                          color: Color(0xFF89b4fa), fontSize: 10)),
                ),
              ],
            ),
          ),
          // 右上の×で個別削除（Stack最前面のGestureDetectorがカードのonTapより先取り）
          Positioned(
            top: 2,
            right: 2,
            child: GestureDetector(
              onTap: () => _delete(id),
              child: Container(
                width: 24,
                height: 24,
                decoration: const BoxDecoration(
                    color: Colors.black54, shape: BoxShape.circle),
                child: const Icon(Icons.close,
                    size: 16, color: Color(0xFFf38ba8)),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
