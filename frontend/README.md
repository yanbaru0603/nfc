# フロントエンドの開発

React・TypeScript・MUIによる画面の雛形と、同一オリジンのJSON APIクライアントを提供する。画面の仕様とAPI一覧の正本は [spec.md](../spec.md) とする。

以下のコマンドは `frontend/` で実行する。依存パッケージはインストール済みのものを使用する。

pnpmの実行バージョンはルートのmise.tomlに従う。pnpm-workspace.yamlの `pmOnFail: ignore` で、既存の `devEngines.packageManager` による起動時の別バージョン解決・自動ダウンロードを止める。`verifyDepsBeforeRun: false` でスクリプト実行前の自動インストールも止め、インストール済みの依存関係をネットワークなしで使用する。package.jsonの `devEngines` 自体は維持する。

この2つはオフラインで実行するための一時的な設定とする。後でオンラインで `mise exec -- pnpm install` を実行してから見直す。

```sh
mise exec -- pnpm dev
```

開発サーバーの画面は `http://127.0.0.1:5173/library` で確認できる。ポートが使用中ならViteが表示するURLを使う。`/api` は既定で `http://127.0.0.1:8000` にプロキシする。接続先のAPIで `/api/session` が利用できると、ライブラリにログイン中のアカウント名が表示される。特典ページとログイン画面では、APIを使って受け取りとアカウント選択を行う。ログイン済みの場合は画面上部からログアウトできる。

プロキシはブラウザから見たオリジンのHostを維持する（`changeOrigin: false`）。Viteのロガーは、画面・APIのURL中の `/r/<token>` を `/r/***` に置き換えてから出力する。

接続先を変更する場合、または同じLANのスマートフォンから確認する場合は環境変数を指定する。

```sh
API_ORIGIN=http://127.0.0.1:8000 VITE_HOST=0.0.0.0 mise exec -- pnpm dev
```

スマートフォンではViteが表示するNetworkのURLを開く。`VITE_HOST` はホスト名・IPアドレス、または `true`（全インターフェース）・`false`（ローカルのみ）を指定できる。

## 検証

```sh
mise exec -- pnpm typecheck
mise exec -- pnpm test
mise exec -- pnpm build
```

ビルド結果は `dist/` に生成する。ローカルでビルド結果だけを確認する場合は `mise exec -- pnpm preview` を使う。APIを含むデモ配信はspec.mdの3.2に従ってFastAPIから行う。

## 共通API

`src/api/types.ts` にAPIの型を置く。`Work`・`Benefit`・`BenefitFile`・`ReceiveResponse` は確定したAPI契約に従う。ログイン・受取・ライブラリ・ダウンロードの型も、確定したAPI契約に従う。

- `getSession({ force? }?): Promise<SessionResponse>`：セッションを取得し、CSRFトークンをメモリに保持
- `apiGet<T>(path, init?): Promise<T>`：JSONを取得
- `apiPost<T>(path, body?, init?): Promise<T>`：JSONを送信し、CSRFトークンを付与
- `ApiError(status, code, message)`：HTTPエラー・通信失敗・不正な応答を共通の例外で通知
- `isApiError(error, status, code?)`：APIのHTTPステータスと任意のコードを判定

`path` は `/api/library` のように `/api` から始まるパスを渡す。通信失敗の `status` は `0`、`code` は `network_error` とする。JSONではない応答や不正なエラー・セッション形式は `invalid_response` とする。成功応答が204、または本文が空なら `undefined` を返す。その他の成功応答はJSONとして解釈し、`T` の実行時検証は呼び出し側で行う。

statusで表示を分けるときは、`isApiError(error, 404)` や `isApiError(error, 403, 'forbidden')` を使う。`invalid_response` はstatusが一致しても偽になり、設定ミスや通信の問題として扱う。例えば、SPAのHTMLに置き換わった404を「特典が見つかりません」と表示しない。

`init` は `{ signal?: AbortSignal }` を受け取る。fetchや本文受信中にsignalが中止された場合は、例外を `ApiError` に変換せずそのまま投げる。`controller.abort('stop')` の文字列や `AbortSignal.timeout()` の `TimeoutError` も維持する。呼び出し側ではuseEffectのクリーンアップで中止し、渡したsignalの `aborted` を確認してエラー表示を省く。POST用の共有セッション取得は中止せず、その完了後も中止済みのPOSTは送信しない。

通常の `getSession()` は進行中の取得に合流する。状態変更後の再取得では `getSession({ force: true })` を使い、古いGETに合流せず新しく取得する。古い応答は、新しいCSRFトークンを上書きしない。

POSTの再送は403かつ `csrf_invalid` の場合だけ、`force: true` でセッションを更新して一度行う。通信失敗では再送しない。受取画面では、失敗後に状態を再取得して確定済みかを確認する。

POSTの成功応答がJSONオブジェクトで、文字列の `csrf_token` を持つ場合は、メモリ上のCSRFトークンを更新する。ログイン・ログアウトの後は、`useSession()` の `refresh()` を呼び出してContextのaccountも更新する。

`SessionProvider` と `useSession()` で `account`、`loading`、`error`、`refresh(): Promise<Account | null>` を共有する。`refresh()` は強制再取得の結果をContextに反映し、取得したaccountを返す。失敗は `error` に格納し、例外も投げるため、呼び出し側で処理する。ログイン画面は再取得の成功とaccountを確認してから戻り先へ遷移する。

`loading` は一度も取得に成功していない間の取得中に真になる。取得成功後の再取得では前のaccountを維持し、loadingに戻さない。取得失敗時は共通レイアウトに再試行可能なエラーを表示し、再試行中はボタンを無効にする。apiPost内のセッション取得結果をSessionProviderへ自動反映する仕組みは、PoCでは設けない。

## 特典ページ

`src/pages/ReceivePage.tsx` は `/r/:token` で特典情報を取得し、spec.mdの6.2に従って表示する。表紙の `work.cover_path` は公開URLのパスとして、そのまま画像の `src` に使う。API取得の中止、通信失敗時の再試行、登録なし受取の確認ダイアログに対応する。

「ログインしてライブラリに追加」は `POST /api/auth/prepare` にトークンを送り、成功後に `/login` へ移動する。ログイン済みの未使用状態では、トークンごとに一度だけ `POST /api/r/<token>/claim/account` を自動送信し、成功後に特典の再生一覧へ移動する。登録なし受取は確認ダイアログで確定し、`POST /api/r/<token>/claim/download` の成功後にダウンロード画面へ移動する。

受取操作の失敗時は確認ダイアログを閉じ、状態を再取得して最新の案内を表示する。409の場合も再取得の結果で表示を切り替える。自動追加は、再取得した状態が未使用でも再送しない。通信に失敗した状態取得は「再試行」で取得し直せる。

## ログインとログアウト

`/login` は `GET /api/auth/accounts` でデモ用アカウントを表示する。選択したIDを `POST /api/auth/login` に送り、セッションの再取得に成功してから応答の `return_to` へ移動する。戻り先は `/` で始まり `//` で始まらないパスに限定し、それ以外は `/library` に移動する。ログイン済みならライブラリへのリンクと、別のアカウントを選ぶためのログアウト案内を表示する。

画面上部には、未ログインなら `/login` へのリンク、ログイン中ならアカウント名とログアウトボタンを表示する。ログアウトは `POST /api/auth/logout` の後にセッションを再取得する。ライブラリと特典の再生一覧にいる場合は `/login` へ移動し、それ以外は同じ画面に留まる。

## ライブラリと再生一覧

`/library` は `GET /api/library` の特典を、表紙と作品名のカードで一覧表示する。カードを選ぶと `/library/<benefit_id>` に移動する。`/library/:benefitId` は `GET /api/library/<benefit_id>` を取得し、再生一覧を表示する。401はログインの案内、404は「この特典は見つかりません」とする。

`src/components/PlaybackList.tsx` は、ライブラリとダウンロードページで共通の再生一覧（spec.mdの6.5）とする。曲の再生URLを返す `fileUrl(file)` とZIPの `archiveUrl` を受け取る。ZIPはdownload属性のない通常の `<a href>` で開き、iPhoneでの保存方法を近くに案内する。画面下部に `<audio controls>` を1つだけ置き、曲を選ぶとsrcを切り替える。曲が終わると次の曲に進み、最後の曲で止まる。audioの `error` で「再生できませんでした」を表示し、`onPlaybackError` で親に知らせる。

## ダウンロードページ

`/r/:token/download` は `GET /api/r/<token>/download` を取得し、再生一覧と再ダウンロードの残り時間を表示する。残り時間は応答の `remaining_seconds` と取得後の経過時間（`performance.now()`）から計算し、端末の時計は使わない。0になると期限切れの表示に切り替える。410は期限切れ、403は受け取ったブラウザで開くよう案内し、どちらにも特典ページへのリンクを添える。404は「この特典は見つかりません」とする。再生に失敗したときは一覧を取り直し、410なら期限切れに切り替える。
