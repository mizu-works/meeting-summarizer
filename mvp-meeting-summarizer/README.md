# 議事録要約・タスク抽出ツール(MVP)

文字起こしテキストを貼り付けると、要約・決定事項・ToDo(担当者・期限つき)を自動生成するミニツール。
ToDoは表で表示され、チェック・Markdownコピー・CSV保存ができる。
「お礼・確認メールの下書きも作る」にチェックすると、決定事項とToDoを確認するメールの下書きも同時に作る(追加の利用回数は消費しない)。

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

## 管理ページ(`/admin`)
- 環境変数 `ADMIN_PASSWORD` を設定すると使える(ユーザー名は何でもよい)。未設定なら 404
- 今月の成功/失敗数、API費用の推定、処理時間、販売したコード数、ウェイトリスト件数、直近14日の推移
- ウェイトリストと利用ログをCSVでダウンロードできる。`data/` は再デプロイで消えるので、デプロイ前に退避する
- 利用ログ(`data/usage_log.csv`)には文字起こしの内容もIPアドレスも記録しない(日時・文字数・処理時間・結果・トークン数のみ)

## API の設定
- タイムアウトは 120 秒・リトライ 1 回(SDK の既定は 10 分・2 回で、ブラウザでは待ちきれないため)
- 環境変数 `CLAUDE_EFFORT`(`low` / `medium` / `high` / `xhigh` / `max`)で考える深さを調整できる。未設定ならモデルの既定(Sonnet 5 は `high`)
  - 下げると速く・安くなるが、品質が落ちることがある。切り替える前に eval で比べる:
    ```
    python eval/run_eval.py --save eval/high.json
    CLAUDE_EFFORT=medium python eval/run_eval.py --save eval/medium.json
    ```

## Stripe Webhook(推奨)
支払い直後にタブを閉じられても、コードが発行・記録されるようにする。

1. Stripe ダッシュボード → 開発者 → Webhook → エンドポイントを追加
   - URL: `https://<本番のドメイン>/stripe/webhook`
   - イベント: `checkout.session.completed` と `checkout.session.async_payment_succeeded`(コンビニ払いなど後から支払いが確定する方法用)
2. 表示される署名シークレット(`whsec_...`)を Render の環境変数 `STRIPE_WEBHOOK_SECRET` に設定

- 未設定なら `/stripe/webhook` は 404 で、従来どおり購入完了ページを開いた時点でコードを発行する
- 購入完了ページと Webhook のどちらが先に来ても、何度来ても、コードは1つだけ
- 「コードが分からない」という問い合わせには、Stripe ダッシュボードで該当の支払いを開き、メタデータの `access_code` を見れば答えられる
