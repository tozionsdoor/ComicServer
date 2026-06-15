import 'package:flutter/material.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter_cache_manager/flutter_cache_manager.dart';

/// 表紙サムネイル。読み込み失敗時に、その画像だけを指数的バックオフで
/// 数回だけ再取得する（接続全体を張り直さず、一斉取得の取りこぼしを救う）。
///
/// 既定回数を使い切っても直らなければ onGaveUp を呼ぶ（呼び出し側で経路の
/// 張り直し＝接続死の保険を1回だけ行う想定）。再試行は画像ごとに上限が
/// あるため、ここだけで無限に回復し続けることはない。
class CoverImage extends StatefulWidget {
  final String imageUrl;
  final Map<String, String> headers;
  final BaseCacheManager cacheManager;
  final BoxFit fit;
  final double? width;
  final Widget placeholder;
  final Widget errorWidget;
  final VoidCallback? onGaveUp;
  final int maxRetries;

  const CoverImage({
    super.key,
    required this.imageUrl,
    required this.headers,
    required this.cacheManager,
    required this.placeholder,
    required this.errorWidget,
    this.fit = BoxFit.cover,
    this.width,
    this.onGaveUp,
    this.maxRetries = 4,
  });

  @override
  State<CoverImage> createState() => _CoverImageState();
}

class _CoverImageState extends State<CoverImage> {
  int  _attempt        = 0;
  bool _retryScheduled = false;
  bool _gaveUp         = false;

  // バックオフ間隔（取りこぼしのバーストを時間方向にばらしてサーバ負荷も和らげる）
  static const _delays = [
    Duration(milliseconds: 400),
    Duration(milliseconds: 900),
    Duration(seconds: 2),
    Duration(seconds: 4),
  ];

  void _onError() {
    if (_retryScheduled || _gaveUp) return;
    if (_attempt >= widget.maxRetries) {
      _gaveUp = true;
      widget.onGaveUp?.call();
      return;
    }
    _retryScheduled = true;
    final d = _delays[_attempt.clamp(0, _delays.length - 1)];
    Future.delayed(d, () {
      if (!mounted) return;
      setState(() {
        _attempt++;
        _retryScheduled = false;
      });
    });
  }

  @override
  Widget build(BuildContext context) {
    return CachedNetworkImage(
      // _attempt を含めることで失敗キャッシュを無効化し、その画像だけ再取得させる
      key: ValueKey('${widget.imageUrl}#$_attempt'),
      imageUrl: widget.imageUrl,
      httpHeaders: widget.headers,
      cacheManager: widget.cacheManager,
      fit: widget.fit,
      width: widget.width,
      placeholder: (_, __) => widget.placeholder,
      errorWidget: (_, __, ___) {
        // ビルド中のsetStateを避け、フレーム後に再試行をスケジュールする
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted) _onError();
        });
        return widget.errorWidget;
      },
    );
  }
}
