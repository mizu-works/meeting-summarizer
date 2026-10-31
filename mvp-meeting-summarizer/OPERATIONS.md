# 運用ガイド

運営者向けの手順をまとめたものです。機能の説明は README を参照してください。

## 環境変数(Render の Environment に設定)

| 変数 | 必須 | 内容 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 必須 | Claude API のキー |
| `STRIPE_SECRET_KEY` | 必須 | Stripe のシークレットキー。制限付きキーなら PaymentIntents の読み書きと Checkout Sessions の読み取り権限 |
| `STRIPE_WEBHOOK_SECRET` | 推奨 | Webhook の署名シークレット(`whsec_...`)。未設定なら購入完了ページを開いた時点でコードを発行する |
| `ADMIN_PASSWORD` | 任意 | 設定すると `/admin` が使える。未設定なら `/admin` は 404 |
| `CLAUDE_EFFORT` | 任意 | `low` / `medium` / `high` / `xhigh` / `max`。未設定ならモデルの既定。変える前に eval で比べる |

## 初回の設定

1. 上の環境変数を Render に設定する
2. Stripe ダッシュボード → 開発者 → Webhook → エンドポイントを追加
   - URL: `https://<ドメイン>/stripe/webhook`
   - イベント: `checkout.session.completed`、`checkout.session.async_payment_succeeded`
   - 表示された署名シークレットを `STRIPE_WEBHOOK_SECRET` に設定
3. Stripe の Payment Link(または Checkout)の完了後の遷移先を
   `https://<ドメイン>/purchase/success?session_id={CHECKOUT_SESSION_ID}` にする
4. `render.yaml` の起動コマンド(`gunicorn app:app --timeout 180 --workers 1 --threads 8`)が Render 側に反映されているか確認する。**`--workers` は 1 のまま**にする(データの保護がプロセス内のロックのため)

## デプロイ前のチェックリスト

Render の無料プランでは、デプロイ・再起動のたびに `data/` が消えます。

- [ ] `/admin` から **ウェイトリストの CSV をダウンロード**して保存した(デプロイで消える)
- [ ] 必要なら利用ログの CSV もダウンロードした
- [ ] プロンプト・スキーマ・モデル・effort を変えた場合は、変更の前後で eval を実行し、合格数が下がっていない
  ```
  cd mvp-meeting-summarizer
  ANTHROPIC_API_KEY=... python eval/run_eval.py --save eval/before.json   # 変更前
  ANTHROPIC_API_KEY=... python eval/run_eval.py --save eval/after.json    # 変更後
  ```
- [ ] GitHub Actions のテストが通っている

アクセスコードは Stripe にバックアップされているので、デプロイで消えても、利用者が入力した時点で自動で復元されます(残り回数も含む)。

## よくある問い合わせ

### 「購入したのにコードが分からない」
1. Stripe ダッシュボードで、その人のメールアドレスで支払いを検索する
2. 支払い(PaymentIntent)を開き、**メタデータの `access_code`** を確認して伝える。`remaining_uses` が残り回数
3. メタデータが空の場合(Webhook 未設定で、購入完了ページも開かれなかった場合):
   その支払いの Checkout Session ID(`cs_...`)を使って
   `https://<ドメイン>/purchase/success?session_id=cs_...` を開くとコードが発行される。そのコードを伝える

### 「コードを入れたのに無効と言われる」
- 入力ミスを確認する。大文字・小文字は区別しない。新しく発行するコードには `0` `O` `1` `I` を使わないが、以前に発行したコードには含まれていることがある
- Stripe ダッシュボードでメタデータの `remaining_uses` が 0 になっていないか確認する
- Stripe の障害中は、デプロイ直後のコード復元ができずに無効と表示されることがある。時間をおいて再試行してもらう
- デプロイ直後は、コードを Stripe から復元する問い合わせが **IP アドレスごとに1時間10回まで**に制限されている(でたらめなコードの大量入力対策)。同じネットワークから多数の人が同時に使うと、一時的に無効と表示されることがある。1時間ほど待つか、運営者が `/purchase/success?session_id=cs_...` を開いて復元する

### 「エラーになった。回数は減った?」
- 結果が出なかった場合(エラー・途中切れ・拒否)は、**回数は自動で戻る**
- `/admin` の「失敗」の内訳で、エラーの種類と件数を確認できる

### 「無料枠が残っているはずなのに上限と言われる」
- 無料枠は IP アドレスごとに月5回。会社のネットワークなどでは、複数人が同じ IP になり枠を共有することがある

## 管理ページの見方(`/admin`)

| 項目 | 見るポイント |
|---|---|
| 失敗 | エラーが増えていれば API キーや Anthropic 側の障害を確認。途中切れが増えていれば入力が長すぎる |
| 形式崩れ(JSON不正) | 通常は 0。増えたらモデルやプロンプトの変更を疑い、eval を実行する |
| API 費用(推定) | トークン数 × 単価の概算。正確な請求額は Anthropic Console で確認 |
| 処理時間 | 中央値が 60 秒を超えるようなら、`CLAUDE_EFFORT` を下げることを検討(eval で品質を確認してから) |

## データの保持

| ファイル(`data/`) | 内容 | 保持 |
|---|---|---|
| `access_codes.csv` | コード、決済セッションID、残り回数 | Stripe にもバックアップ |
| `free_usage.csv` | IP アドレス、利用月 | 当月分のみ。翌月最初の利用時に前月以前を削除 |
| `usage_log.csv` | 日時、文字数、処理時間、結果、トークン数(本文・IP なし) | デプロイまで |
| `waitlist.csv` | メールアドレス、お仕事、使いたい場面 | デプロイまで(事前にダウンロード) |

文字起こしの本文はどこにも保存しません。
