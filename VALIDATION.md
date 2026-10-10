# 日本語対応 Fork の検証記録

2026-10-10、クラウド環境。実機の検証結果ではありません。
実装ブランチ: `feat/japanese-metadata-upstream-sync`。
開始時の Fork / upstream main: `d108bea67845f9eda4563704dad8c99e525deceb`。

| 検証 | 結果 |
| --- | --- |
| Python 3.13.5 / pytest、既存＋日本語化 | 661 passed、ライブ 10 件 deselected |
| 実 music-assistant-models 1.1.218 | 32 passed |
| 一時 Git リポジトリで同期、競合、ローカル変更保護、既存ブランチ保護 | 5 passed |
| 両インストーラの release-only 解決（取得失敗／安定タグ／ref 併用） | 1 passed（両スクリプトを検証） |
| dash / bash インストーラ・Watcher | 各 91 / 47 / 12 passed、失敗・skip なし |
| Docker ビルドマトリクスの既存シェルテスト | 39 passed |
| compileall / Ruff E9,F63,F7,F82 / ShellCheck | 成功 |
| Actionlint 1.7.7（公式配布物の SHA-256 確認済み） | 成功 |
| stable Music Assistant amd64 イメージへの組み込みビルド | 成功 |
| stable の実サーバー（Python 3.14）でインポート・認証通知契約 | 成功 |
| 実サーバーで言語オプション、言語別キャッシュ、従来キーの排除、インスタンス分離、再生時のキャッシュ迂回 | 成功 |

サーバーベースの digest:
`ghcr.io/music-assistant/server@sha256:45fdb050e06962097a07feeb092606ad119fac17965c8228a57146448d5fa772`。
最後のサーバー検証は、そのビルド済みイメージに最終の `ytmusic_free` を読み取り専用で
マウントして実行しています。クラウドの Docker は vfs を使用し、反復ビルドで
容量が不足したため、この作業で作った古い検証イメージと未使用ビルドキャッシュを
削除して再開しました。リポジトリや MA の設定は削除していません。

日本語 API テストは実際の ytmusicapi 1.12.3 を使い、ネットワーク送信だけを置き換えています。
日本語と英語の `hl` / `gl`、HTTP ヘッダー、Cookie・ブランドアカウント・アカウント番号、
既定の 30 秒タイムアウトを確認しました。実 Cookie は使用していません。
パーサー・検索・ライブラリ同期・yt-dlp フォールバックはフィクスチャで検証しました。

未検証・外部で必要な確認:

- YouTube の実メタデータ、ストリーム取得、実再生。`music.youtube.com` への CONNECT が
  HTTP 403 で拒否されたため、ライブテストを実行していません。
- HAOS 18 系での表示、Cookie 実認証、同期、コンテナ再作成後の自動復旧、実ロールバック。
- Alpine 3.20 / BusyBox のテスト。イメージ取得は成功しましたが
  `dl-cdn.alpinelinux.org` の APKINDEX に接続できず依存導入に失敗しました。
  既存のプロキシとホスト CA を使う再試行でも改善しませんでした。TLS 検証は無効化していません。
- GitHub Actions の実行、App 認証による同期 PR 作成、自動マージ、リリース公開。
  GitHub API へのリクエストが Forbidden のため、リモート設定を変更・確認していません。
- beta/nightly サーバーと arm64。stable の amd64 だけをこの環境で検証しました。

GitHub App、Variables、Secret、ブランチ保護の有効化手順は README にあります。
クラウドから追加検証する場合に必要な接続先は `api.github.com`、
`music.youtube.com`、`www.youtube.com`、`youtubei.googleapis.com`、
`*.googlevideo.com`（メディア）、`dl-cdn.alpinelinux.org` です。
通信許可だけでは Cookie 認証や YouTube からの接続許可は保証されません。
