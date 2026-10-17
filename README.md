# NFCグッズ特典受け取り PoC

NFCグッズ（NTAG215）をスマートフォンにかざして、特典の音源をアカウントに追加するか、登録せずに一回だけダウンロードするPoC。仕様は [spec.md](spec.md)、フロントエンドの詳細は [frontend/README.md](frontend/README.md) を参照する。

## 環境の準備

Python・Poetry・Node.js・pnpmのバージョンは `mise.toml` で固定している。Pythonの依存関係はPoetryでプロジェクト内の `.venv` に入れる。

```sh
mise install
mise exec -- poetry sync              # Pythonの依存関係
mise exec -- pnpm -C frontend install # フロントエンドの依存関係
.venv/bin/python seed.py              # DB（instance/app.db）・音源（media/）・デモ用アカウントを用意
```

`seed.py` は再実行しても受取状態を維持し、開発用の受け取りURL（NFCなしで開ける）を表示する。開発用のトークンを未使用に戻すときは、サーバーと発行CLIを止めてから `--reset` でDBを作り直す。

設定は環境変数で指定する（`backend/config.py`）。発行CLIとサーバーで同じ値を使う。

| 環境変数 | 既定値 | 内容 |
|---|---|---|
| `BASE_URL` | `http://localhost:5173` | タグに書き込むURLの基点。スマートフォンから開くURLと同じにする |
| `COOKIE_SECURE` | `BASE_URL` が `https` なら `true` | CookieのSecure属性（`true` / `false`） |
| `RETRY_TTL_SECONDS` | `600` | 登録なしで受け取った後の再試行期限（秒） |
| `SESSION_SECRET` | `instance/session_secret` に自動生成 | セッションCookieの署名鍵 |
| `DB_PATH` / `MEDIA_DIR` | `instance/app.db` / `media` | 相対パスはプロジェクトルート基準 |

## デモ（ビルドしてFastAPIから配信）

フロントエンドをビルドし、Uvicorn 1つで画面とAPIを配信する。Node.jsの常駐は不要。

```sh
mise exec -- pnpm -C frontend build   # frontend/dist に出力
export BASE_URL=http://192.168.1.23:8000 RETRY_TTL_SECONDS=30
.venv/bin/python serve.py             # --host 0.0.0.0 --port 8000 が既定
```

`serve.py` は自動リロードなしで起動し、アクセスログの `/r/<token>`（`/api/r/<token>` を含む）を `/r/***` に伏せる。起動時に `BASE_URL` を表示し、`frontend/dist` がなければビルドを促す警告を出す。画面URL（`/r/<token>`、`/library` など）の直接アクセス・再読み込みには `index.html` を返す。

### スマートフォンからの接続

- 同じWi-Fi：MacのIPアドレス（`ipconfig getifaddr en0`）で `BASE_URL=http://<IP>:8000` とする。HTTPなのでCookieのSecureは付かない。会場のWi-Fiでは端末同士の通信が禁止されていることがある
- トンネル：Cloudflareの名前付きトンネルなど、URLが固定されるものを使い、接続先を `http://localhost:8000` にする。`BASE_URL=https://<固定のホスト>` とすると `COOKIE_SECURE` は自動で `true` になる。クイックトンネル（`trycloudflare.com`）は起動のたびにURLが変わるので使わない。トンネル側のログにもURLが残る点に注意する

`COOKIE_SECURE=true` のままHTTPで開くとログインや再試行ができない。HTTPSでない接続先に切り替えたら `BASE_URL` を合わせる。

## NFCタグの発行

RC-S380をMacにつなぎ、サーバーと同じ `BASE_URL` で実行する。タグに書いたURLのホストは後から変えられないので、デモのURLを決めてから発行する。

```sh
.venv/bin/python issue.py --benefit benefit-001             # 1個発行
.venv/bin/python issue.py --benefit benefit-001 --count 5   # 連続発行（1個ごとにタグを置き換える）
.venv/bin/python issue.py --benefit benefit-001 --reissue   # 発行済み・受け取り済みのタグを書き直す
```

同じUIDに有効な取得権があると中止する。`--reissue` では書き込みと読み直しに成功してから既存の取得権を無効にする。`BASE_URL` を変えたときも `--reissue` で書き直す。

## デモの流れ

1. 事前にスマートフォンで、NFCのURLを開く既定のブラウザ（iPhoneならSafari）を使ってログインしておく。ログイン状態はブラウザごとに保存されるため、別のブラウザでは自動追加を実演できない
2. ログイン済みでタグをかざすと、特典ページが自動でアカウントに追加し、再生一覧に移動する
3. ログアウトしてから別のタグをかざし、「登録せずに一回だけダウンロード」を選ぶ。`RETRY_TTL_SECONDS=30` なら、30秒後に同じページを開き直すと期限切れになる
4. 受け取り済みのタグをかざすと「すでに受け取られています」になる。やり直すときは `--reissue` で書き直す

## 開発

UvicornとViteの開発サーバーを別々に起動し、ブラウザではViteの `http://127.0.0.1:5173` を開く。Viteが `/api` をUvicornにプロキシする。

```sh
.venv/bin/uvicorn backend.main:create_app --factory --reload --no-access-log   # API（:8000）
mise exec -- pnpm -C frontend dev                                             # 画面（:5173）
```

`backend.main:create_app` をそのまま起動した場合はSPAを配信しない。Uvicornの標準のアクセスログは受取トークンを伏せないため、開発時は `--no-access-log` で止める（Viteのログは伏せる）。同じLANのスマートフォンから確認するときは、`VITE_HOST=0.0.0.0` でViteを公開し、`BASE_URL=http://<MacのIP>:5173` で発行・シードする。

### テスト

```sh
.venv/bin/python -m unittest discover -v   # バックエンド・発行処理
mise exec -- pnpm -C frontend test         # フロントエンド
mise exec -- pnpm -C frontend typecheck
```

## Suica読み取りサンプル（RC-S380の動作確認）

`read_suica.py` は、RC-S380でSuicaのIDm、PMm、最新の利用履歴に記録された残高を1回表示する。カードへの書き込みは行わない。Suicaと同じシステム・履歴形式の交通系ICカードも対象になり得る。

```sh
.venv/bin/python read_suica.py              # 30秒以内に検出すると結果を表示して終了（Ctrl+Cでも終了）
.venv/bin/python read_suica.py --timeout 60 # 待機時間を変更
```

残高は履歴サービス`0x090F`のブロック0から読み取る。これは最新履歴に記録された残高で、履歴が空の場合は表示できない。IDmはカードの識別子で、券面のSuica番号とは異なる。`libusb-package`に含まれるUSBライブラリを使うため、Homebrewへの追加は不要。

USB通信に失敗する場合は、接続を確認し、リーダーを使用しているほかのアプリを終了する。カードを検出できない場合は、ほかのカードを離し、Suicaの位置を調整して再実行する。

- [nfcpy公式ドキュメント](https://nfcpy.readthedocs.io/en/latest/)
- [Suicaの利用履歴の読み取りとデータ形式](https://yamori-tech.hatenablog.com/entry/suica-data-readout)
