# 議事録要約・タスク抽出ツール(MVP)

文字起こしテキストを貼り付けると、要約・決定事項・ToDo(担当者・期限つき)を自動生成するミニツール。
ToDoは表で表示され、チェック・Markdownコピー・CSV保存ができる。

## セットアップ

```
pip install -r requirements.txt
```

## 起動

```
ANTHROPIC_API_KEY=xxxxx python app.py
```

`http://localhost:5000` にアクセス。

## テスト

```
pytest test_app.py -v
```

## 設計方針
- 文字起こし・録音機能は持たない(テキスト入力のみ)。守秘義務への配慮と開発コスト削減が理由
- 処理したテキストはサーバーに保存しない(ステートレス)
- 詳細な設計背景は `.company/engineering/docs/ai-meeting-summary-mvp-design.md` を参照

## 早期アクセス受け皿
- `/waitlist` に申し込みフォームを用意(メールアドレス・お仕事・使いたい場面)
- 送信内容は `data/waitlist.csv` に追記保存(`.gitignore`済み、リポジトリには含めない)

## アクセスコードの保存(Stripe へのバックアップ)
- アクセスコードは `data/access_codes.csv` に保存されるが、Render の無料プランでは再デプロイ・再起動で消える
- そのため、コードと残り回数を支払いの Stripe PaymentIntent の metadata(`access_code` / `remaining_uses` / `checkout_session_id`)にも書き込んでいる
- ローカルに見つからないコードが入力されたら、Stripe Search API で探して復元する。追加費用なし
- 必要な環境変数: `STRIPE_SECRET_KEY`(制限付きキーを使う場合は PaymentIntents の読み書きと Checkout Sessions の読み取り権限が必要)

## 品質チェック(eval)
プロンプト・スキーマ・モデルを変えるときは、変更の前後で実行して合格数を比べる。

```
ANTHROPIC_API_KEY=xxxxx python eval/run_eval.py              # 全ケース(数セント程度)
python eval/run_eval.py --case zoom_vtt_raw --repeat 3       # 1ケースを3回(出力は毎回少し変わる)
python eval/run_eval.py --save eval/results.json             # 出力を保存
```

- ケースは `eval/cases/*.json`。文字起こしと、出力が満たすべき条件(担当者・期限・件数など)を書く
- アプリと同じ `app.summarize()` を呼ぶので、本番と同じプロンプト・モデルで確認できる
- 条件そのものの正しさは `test_eval.py` で確認している(APIは呼ばない)
