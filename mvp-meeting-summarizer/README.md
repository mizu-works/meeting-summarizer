# 議事録要約・タスク抽出ツール(MVP)

文字起こしテキストを貼り付けると、要約・決定事項・Next Actionを自動生成するミニツール。

## セットアップ

```
pip install -r requirements.txt
```

## 起動

```
ANTHROPIC_API_KEY=xxxxx python app.py
```

`http://localhost:5000` にアクセス。

## 設計方針
- 文字起こし・録音機能は持たない(テキスト入力のみ)。守秘義務への配慮と開発コスト削減が理由
- 処理したテキストはサーバーに保存しない(ステートレス)
- 詳細な設計背景は `.company/engineering/docs/ai-meeting-summary-mvp-design.md` を参照

## 早期アクセス受け皿
- `/waitlist` に申し込みフォームを用意(メールアドレス・お仕事・使いたい場面)
- 送信内容は `data/waitlist.csv` に追記保存(`.gitignore`済み、リポジトリには含めない)
