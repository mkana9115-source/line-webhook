# LINE公式アカウントWebhookサーバー
#
# 機能: LINEお客様メッセージ受信 → AI返信案生成 → Gmail通知
# 起動: gunicorn app:app (本番) / python app.py (開発確認)
# 必要環境変数:
#   LINE_CHANNEL_SECRET  - LINE DevelopersコンソールのChannel Secret
#   GMAIL_USER           - 送信元Gmailアドレス
#   GMAIL_APP_PASSWORD   - GmailのアプリパスワードI(16文字)
#   NOTIFY_EMAIL         - 通知先メールアドレス（省略時はGMAIL_USERと同じ）
#   ANTHROPIC_API_KEY    - Claude AI APIキー

import os
import hmac
import hashlib
import base64
import json
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify, abort
from dotenv import load_dotenv
import anthropic

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
GMAIL_USER          = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD  = os.getenv("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL        = os.getenv("NOTIFY_EMAIL") or GMAIL_USER
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")

MSG_LABELS = {
    "text":     "テキスト",
    "image":    "画像",
    "video":    "動画",
    "audio":    "音声",
    "file":     "ファイル",
    "location": "位置情報",
    "sticker":  "スタンプ",
}

SYSTEM_PROMPT = """あなたはネットショップの公式LINEカスタマーサポートアシスタントです。
お客様からのメッセージを読んで、店舗オーナーが送る丁寧な返信案を1つ提案してください。

返信のガイドライン:
- 返品・交換の問い合わせ → まず注文番号と理由を確認する
- 配送・発送の問い合わせ → 注文番号を確認する
- 商品の在庫・詳細の問い合わせ → 具体的な商品名を確認する
- クレーム・不満 → 誠実にお詫びし、解決策を提示する
- 常に丁寧で温かみのある言葉遣いを使う（「〜でございます」調）
- 返信案のみを出力し、説明や前置きは不要"""


def _verify_signature(body: bytes, sig: str) -> bool:
    """LINEプラットフォームからのリクエストか検証する"""
    digest = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), sig)


def _suggest_reply(customer_message: str) -> str:
    """Claude AIにお客様メッセージを渡して返信案を生成する"""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": customer_message}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error("AI返信案生成失敗: %s", e)
        return "（AI返信案の生成に失敗しました）"


def _send_gmail(subject: str, text: str) -> None:
    """Gmail SMTPで通知メールを送信する"""
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_USER
    msg["To"]      = NOTIFY_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(text, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)
    logger.info("通知メール送信完了: %s → %s", subject, NOTIFY_EMAIL)


@app.route("/webhook", methods=["POST"])
def webhook():
    sig  = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not _verify_signature(body, sig):
        logger.warning("署名検証失敗 — 不正なリクエストの可能性があります")
        abort(400, "Invalid signature")

    for event in json.loads(body).get("events", []):
        if event.get("type") != "message":
            continue

        m     = event["message"]
        uid   = event.get("source", {}).get("userId", "不明")
        mtype = m.get("type", "不明")
        label = MSG_LABELS.get(mtype, mtype)

        if mtype == "text":
            customer_text = m.get("text", "")
            ai_reply      = _suggest_reply(customer_text)
            subject       = "【公式LINE】新しいメッセージが届きました"
            body_text     = (
                f"公式LINEにお客様からメッセージが届きました。\n\n"
                f"送信者ID : {uid}\n"
                f"内容     : {customer_text}\n\n"
                f"{'─' * 30}\n"
                f"【AIが提案する返信案】\n\n"
                f"{ai_reply}\n\n"
                f"{'─' * 30}\n"
                "※この返信案はAIによる提案です。内容をご確認の上、LINEアプリから手動で送信してください。"
            )
        else:
            subject   = f"【公式LINE】{label}が届きました"
            body_text = (
                f"公式LINEにお客様から{label}が届きました。\n\n"
                f"送信者ID : {uid}\n\n"
                "LINEアプリでご確認・ご返信ください。"
            )

        try:
            _send_gmail(subject, body_text)
        except Exception as e:
            logger.error("メール送信失敗: %s", e)

    return jsonify({"status": "ok"})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "running"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
