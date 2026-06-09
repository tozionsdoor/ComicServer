import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';

/// TLS自己署名証明書のフィンガープリント（SHA-256 of DER, hex）を検証する
/// HTTP クライアントを作る。fingerprint が空の場合は TOFU モード（任意の証明書を受け入れ）。
http.Client makePinnedClient(String fingerprint) {
  final httpClient = HttpClient();
  if (fingerprint.isNotEmpty) {
    httpClient.badCertificateCallback = (cert, host, port) {
      final fp = sha256.convert(cert.der).toString();
      return fp == fingerprint;
    };
  } else {
    // 初回接続 or 手動URL入力時: 自己署名証明書を受け入れる（TOFU）
    httpClient.badCertificateCallback = (cert, host, port) => true;
  }
  return IOClient(httpClient);
}
