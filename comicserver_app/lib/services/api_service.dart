import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/book.dart';

class ApiService {
  String baseUrl;
  String username;
  String password;

  ApiService({
    required this.baseUrl,
    required this.username,
    required this.password,
  });

  String get _auth =>
      'Basic ${base64Encode(utf8.encode('$username:$password'))}';

  Map<String, String> get headers => {'Authorization': _auth};

  String coverUrl(String bookId) => '$baseUrl/api/books/$bookId/cover';
  String pageUrl(String bookId, int page) =>
      '$baseUrl/api/books/$bookId/pages/$page';

  Future<bool> testConnection() async {
    try {
      final res = await http
          .get(Uri.parse('$baseUrl/api/status'), headers: headers)
          .timeout(const Duration(seconds: 5));
      return res.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  Future<FolderContents> getFolders(String path) async {
    final uri = Uri.parse(
        '$baseUrl/api/folders?path=${Uri.encodeQueryComponent(path)}');
    final res = await http.get(uri, headers: headers);
    if (res.statusCode != 200) throw Exception('HTTP ${res.statusCode}');
    return FolderContents.fromJson(jsonDecode(res.body));
  }

  Future<BookInfo> getBookInfo(String bookId) async {
    final res = await http.get(
        Uri.parse('$baseUrl/api/books/$bookId/info'),
        headers: headers);
    if (res.statusCode != 200) throw Exception('HTTP ${res.statusCode}');
    return BookInfo.fromJson(jsonDecode(res.body));
  }
}
