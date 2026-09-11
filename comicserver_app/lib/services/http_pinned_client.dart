import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';

/// TLS自己署名証明書のフィンガープリント（SHA-256 of DER, hex）を検証する
/// HTTP クライアントを作る。fingerprint が空の場合は TOFU モード（任意の証明書を受け入れ）。
http.Client makePinnedClient(String fingerprint) {
  final httpClient = HttpClient();
  // 使い終わった接続をプールに寝かせる時間を、サーバーが接続を切るまでの
  // 時間より短くする。
  //
  // HTTP/1.1 の keep-alive はサーバー側が好きな時に接続を切ってよい仕様だが、
  // dart:io の HttpClient はプールから取り出した接続が既に切られていても
  // 張り直してくれず、GET の再送もしない（掴んだ瞬間に
  // HttpException: Connection closed before full header was received）。
  // uvicorn の既定は5秒で切る / dart:io の既定は15秒プールに残すので、
  // その差の10秒間に来た最初の1本は必ず死ぬ。
  // 書棚の表紙取得から数秒〜十数秒空けて本を開いた時の「1ページ目」が
  // ちょうどここに当たり、ブロークンアイコンの直接の原因だった
  // （実測: アイドル5/7/10/14秒で12回中4回失敗。idleTimeout短縮で0件）。
  //
  // 先に手を離すのを常にこちら側にすれば、死んだ接続はプールに残らない。
  // 読書中はリクエストが数秒おきに続くので接続の使い回しは効いたままで、
  // 余分なハンドシェイクは「しばらく操作していなかった時の1本目」だけ。
  httpClient.idleTimeout = const Duration(seconds: 4);
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
