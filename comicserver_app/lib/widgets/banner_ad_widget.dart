import 'package:flutter/material.dart';
import 'package:google_mobile_ads/google_mobile_ads.dart';
import '../services/ads_service.dart';

/// 画面下部に表示するバナー広告。
/// 「広告削除」購入済み、または広告の読み込みに失敗した場合は何も表示しない。
class BannerAdWidget extends StatefulWidget {
  const BannerAdWidget({super.key});

  @override
  State<BannerAdWidget> createState() => _BannerAdWidgetState();
}

class _BannerAdWidgetState extends State<BannerAdWidget> {
  BannerAd? _ad;
  bool _hidden = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (await AdsService.adsRemoved()) {
      if (mounted) setState(() => _hidden = true);
      return;
    }
    final ad = BannerAd(
      adUnitId: AdsService.bannerAdUnitId,
      size: AdSize.banner,
      request: const AdRequest(),
      listener: BannerAdListener(
        onAdLoaded: (ad) {
          if (mounted) setState(() => _ad = ad as BannerAd);
        },
        onAdFailedToLoad: (ad, error) {
          ad.dispose();
          if (mounted) setState(() => _hidden = true);
        },
      ),
    );
    ad.load();
  }

  @override
  void dispose() {
    _ad?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final ad = _ad;
    // エッジツーエッジ表示でナビゲーションバーが透過のため、その分の余白を確保する。
    // 広告が無い間（読込失敗・広告削除ビルド）も高さ0にはせず、ナビバー分だけは
    // 確保する（高さ0だとbottomNavigationBarの安全域処理が効かず、本棚の中身が
    // ナビゲーションバーの下に潜り込んでしまうため）。
    final bottomInset = MediaQuery.of(context).padding.bottom;
    if (_hidden || ad == null) {
      return SizedBox(height: bottomInset);
    }
    return SafeArea(
      top: false,
      left: false,
      right: false,
      child: Container(
        width: double.infinity,
        height: ad.size.height.toDouble(),
        color: const Color(0xFF181825),
        alignment: Alignment.center,
        child: SizedBox(
          width: ad.size.width.toDouble(),
          height: ad.size.height.toDouble(),
          child: AdWidget(ad: ad),
        ),
      ),
    );
  }
}
