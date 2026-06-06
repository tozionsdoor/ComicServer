import 'package:flutter/material.dart';
import '../services/api_service.dart';

/// 画面下部に出る「再接続中…」バナー。
/// [api].status が非nullの間だけ表示する。Stack の子として置くこと
/// （表示時は Positioned、非表示時はゼロサイズを返す）。
class ReconnectBanner extends StatelessWidget {
  final ApiService api;
  const ReconnectBanner({super.key, required this.api});

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<String?>(
      valueListenable: api.status,
      builder: (context, msg, _) {
        if (msg == null) return const SizedBox.shrink();
        return Positioned(
          left: 0,
          right: 0,
          bottom: 0,
          child: IgnorePointer(
            child: Container(
              padding: EdgeInsets.only(
                top: 10,
                bottom: 10 + MediaQuery.of(context).padding.bottom,
              ),
              color: const Color(0xE6181825),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const SizedBox(
                    width: 15,
                    height: 15,
                    child: CircularProgressIndicator(
                        strokeWidth: 2, color: Color(0xFF89b4fa)),
                  ),
                  const SizedBox(width: 12),
                  Text(msg,
                      style: const TextStyle(
                          color: Color(0xFFcdd6f4), fontSize: 13)),
                ],
              ),
            ),
          ),
        );
      },
    );
  }
}
