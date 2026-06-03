class FolderItem {
  final String name;
  final String path;
  final int count;
  final List<String> previewIds;

  FolderItem({
    required this.name,
    required this.path,
    required this.count,
    required this.previewIds,
  });

  factory FolderItem.fromJson(Map<String, dynamic> j) => FolderItem(
        name: j['name'] as String,
        path: j['path'] as String,
        count: j['count'] as int,
        previewIds: List<String>.from(j['ids'] as List),
      );
}

class BookItem {
  final String id;
  final String title;
  final String rel;

  BookItem({required this.id, required this.title, required this.rel});

  factory BookItem.fromJson(Map<String, dynamic> j) => BookItem(
        id: j['id'] as String,
        title: j['title'] as String,
        rel: j['rel'] as String? ?? '',
      );
}

class FolderContents {
  final String path;
  final List<FolderItem> folders;
  final List<BookItem> books;

  FolderContents({required this.path, required this.folders, required this.books});

  factory FolderContents.fromJson(Map<String, dynamic> j) => FolderContents(
        path: j['path'] as String,
        folders: (j['folders'] as List).map((e) => FolderItem.fromJson(e)).toList(),
        books: (j['books'] as List).map((e) => BookItem.fromJson(e)).toList(),
      );
}

class BookInfo {
  final String id;
  final String title;
  final int count;

  BookInfo({required this.id, required this.title, required this.count});

  factory BookInfo.fromJson(Map<String, dynamic> j) => BookInfo(
        id: j['id'] as String,
        title: j['title'] as String,
        count: j['count'] as int,
      );
}
