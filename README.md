# Suica読み取りサンプル

RC-S380を使って、SuicaのIDm、PMm、最新の利用履歴に記録された残高を1回表示します。
カードへの書き込みは行いません。Suicaと同じシステム・履歴形式の交通系ICカードも対象になり得ます。

## 実行

このフォルダで次のコマンドを実行し、RC-S380にSuicaを1枚かざしてください。
30秒以内に検出すると結果を表示して終了します。Ctrl+Cでも終了できます。

```sh
.venv/bin/python read_suica.py
```

待機時間を長くする場合は、秒数を指定します。

```sh
.venv/bin/python read_suica.py --timeout 60
```

残高は履歴サービス`0x090F`のブロック0から読み取ります。
これは最新履歴に記録された残高です。履歴が空の場合は残高を表示できません。
IDmはカードの識別子で、券面のSuica番号とは異なります。

## 環境の再作成

既存のPython 3.14で実機の読み取りを確認済みです。依存ライブラリは`.venv`内にインストールします。
`libusb-package`に含まれるUSBライブラリを使うため、Homebrewへの追加は不要です。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## 確認

```sh
.venv/bin/python -m unittest -v
.venv/bin/python -m compileall -q read_suica.py test_read_suica.py
```

USB通信に失敗する場合は、接続を確認し、リーダーを使用しているほかのアプリを終了してください。
カードを検出できない場合は、ほかのカードを離し、Suicaの位置を調整して再実行してください。

## 参考

- [nfcpy公式ドキュメント](https://nfcpy.readthedocs.io/en/latest/)
- [Suicaの利用履歴の読み取りとデータ形式](https://yamori-tech.hatenablog.com/entry/suica-data-readout)
