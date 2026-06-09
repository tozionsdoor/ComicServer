import 'dart:math';
import 'package:shared_preferences/shared_preferences.dart';

/// 端末固有IDの管理。初回インストール時に UUID v4 を生成して永続保存する。
class DeviceService {
  static Future<String> getDeviceId() async {
    final prefs = await SharedPreferences.getInstance();
    var id = prefs.getString('device_id');
    if (id == null) {
      id = _generateUuid();
      await prefs.setString('device_id', id);
    }
    return id;
  }

  static const String deviceName = 'Android';

  static String _generateUuid() {
    final rand  = Random.secure();
    final bytes = List<int>.generate(16, (_) => rand.nextInt(256));
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 1
    final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
    return '${hex.substring(0, 8)}-${hex.substring(8, 12)}'
        '-${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
  }
}
