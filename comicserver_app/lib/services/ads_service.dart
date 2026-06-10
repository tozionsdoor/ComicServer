import 'dart:async';

import 'package:google_mobile_ads/google_mobile_ads.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// AdMob広告の初期化・同意取得・表示要否を一元管理する。
class AdsService {
  AdsService._();

  /// ビルド時に `--dart-define=NO_ADS=true` を指定すると、広告SDKの初期化を含め
  /// 広告関連の処理を一切行わない「広告なしビルド」になる
  /// （自分用の別APKを配布するための仕組み）。
  static const bool _noAdsBuild = bool.fromEnvironment('NO_ADS');

  /// バナー広告ユニットID（ArcHive本番ID）。
  static const String bannerAdUnitId =
      'ca-app-pub-4714141360260604/7977088169';

  /// インタースティシャル（全画面）広告ユニットID（ArcHive本番ID）。
  static const String _interstitialAdUnitId =
      'ca-app-pub-4714141360260604/5854026701';

  /// 「本を開く」操作（本棚/履歴からのタップ、またはリーダー内の次の巻/前の巻）を
  /// 合計何回行ったらインタースティシャル広告1回分の「権利」が貯まるか。
  /// 「次へ」を使わず本棚から本を開くだけの使い方でも、また連続読み（ビンジ）で
  /// 本棚に戻る回数が少なくても、自然に広告が挟まるようにする。
  static const int _volumeNavsPerAd = 3;

  static const String _prefsKeyAdsRemoved = 'ads_removed';

  static InterstitialAd? _interstitial;

  /// 「本を開く」操作（本棚/履歴オープン＋次の巻/前の巻）の累計のうち、
  /// まだ広告に消化されていない分。
  static int _volumeNavCount = 0;

  /// AdMob SDKの初期化。main()で一度だけ呼ぶ。
  /// 「広告なしビルド」では何もしない（SDK自体を初期化しない）。
  static Future<void> initialize() async {
    if (_noAdsBuild) return;
    await MobileAds.instance.initialize();
    _loadInterstitial();
  }

  static void _loadInterstitial() {
    InterstitialAd.load(
      adUnitId: _interstitialAdUnitId,
      request: const AdRequest(),
      adLoadCallback: InterstitialAdLoadCallback(
        onAdLoaded: (ad) => _interstitial = ad,
        onAdFailedToLoad: (_) => _interstitial = null,
      ),
    );
  }

  /// リーダー内で「次の巻/前の巻」に進んだ時に呼ぶ。
  /// [maybeShowInterstitial]内での「本を開く」カウントと合算され、
  /// 一定回数に達するとインタースティシャル広告の表示権利が1回分貯まる。
  static void recordVolumeRead() {
    if (_noAdsBuild) return;
    _volumeNavCount++;
  }

  /// 本を開く直前（本棚・履歴からのタップ）に呼ぶ。この呼び出し自体も
  /// 「本を開く」操作として1回分カウントしたうえで、累計回数が既定値未満・
  /// 「広告削除」購入済み・広告未読込のいずれかなら何もせず即座に返る
  /// （未読込の場合は権利を消費せず持ち越し、次回の本棚オープン時に再度試す）。
  /// 条件を満たし広告が読み込み済みなら全画面広告を表示し、
  /// 閉じられる（または表示失敗する）まで待ってから返る。
  static Future<void> maybeShowInterstitial() async {
    recordVolumeRead(); // 本を開く操作自体も1回としてカウントする
    if (_volumeNavCount < _volumeNavsPerAd) return;
    if (await adsRemoved()) return;

    final ad = _interstitial;
    if (ad == null) return; // 未読込: 権利は持ち越して次回再挑戦
    _volumeNavCount = 0;
    _interstitial = null;

    final completer = Completer<void>();
    ad.fullScreenContentCallback = FullScreenContentCallback(
      onAdDismissedFullScreenContent: (ad) {
        ad.dispose();
        _loadInterstitial();
        if (!completer.isCompleted) completer.complete();
      },
      onAdFailedToShowFullScreenContent: (ad, error) {
        ad.dispose();
        _loadInterstitial();
        if (!completer.isCompleted) completer.complete();
      },
    );
    await ad.show();
    return completer.future;
  }

  /// EEA/UK等、UMP同意フォームが必要な地域のユーザーにのみ表示する。
  /// 対象外の地域では何もしない。失敗しても広告表示は継続する。
  /// 「広告なしビルド」では何もしない。
  static void requestConsent() {
    if (_noAdsBuild) return;
    final params = ConsentRequestParameters();
    ConsentInformation.instance.requestConsentInfoUpdate(
      params,
      () {
        ConsentForm.loadAndShowConsentFormIfRequired((_) {
          // 同意取得完了 or 不要。エラーでも広告表示は継続する。
        });
      },
      (_) {
        // 取得失敗。広告表示は継続する。
      },
    );
  }

  /// 「広告削除」購入済みかどうか。将来の課金実装が購入成功時にここを true にする。
  /// 「広告なしビルド」では常にtrue。
  static Future<bool> adsRemoved() async {
    if (_noAdsBuild) return true;
    final prefs = await SharedPreferences.getInstance();
    return prefs.getBool(_prefsKeyAdsRemoved) ?? false;
  }
}
