import os
import json
import hmac
import hashlib
import time
import requests
from flask import Flask, request

app     = Flask(__name__)
api_key = os.environ["ANTHROPIC_API_KEY"]

LBANK_API_KEY    = os.environ.get("LBANK_API_KEY",    "")
LBANK_SECRET_KEY = os.environ.get("LBANK_SECRET_KEY", "")
LBANK_BASE       = "https://api.lbkex.com"

TRADE_SIZE_A     = 10.0
TRADE_SIZE_AP    = 20.0

# ================================================================
# ESS FLOKI 8X SYSTEM PROMPT
# ================================================================
ESS_SYSTEM = """You are the ESS FLOKI 8X Swing Trading System analyst.

CORE RULE: BTC determines market direction. FLOKI is ONLY traded LONG
when BTC supports the setup. NO SHORTS. NO counter-trend. NO chasing.
NO FOMO entries.

REQUIRED 5-STEP SEQUENCE (all must be confirmed):
1. Liquidity Sweep -- wick through key level, body closes back inside
2. Reclaim -- price closes back above the swept level
3. Retest Hold -- price returns to reclaimed level and holds above it
4. BTC Confirmation -- BTC holding support, not aggressively selling
5. Entry Trigger -- bullish confirmation candle on 15M timeframe

IF ANY STEP IS MISSING: output STATUS = WAITING. DO NOT trigger entry.

SETUP TYPES:
Setup A:  BTC stabilizing. Partial position 25-50%. Min R:R 1:3.
Setup A+: BTC confirmed recovery + expanding momentum + strong reclaim.
          Full position. Min R:R 1:4. Preferred 1:6 to 1:8+.

NO TRADE CONDITIONS (output STATUS = NO TRADE if ANY apply):
- No BTC confirmation
- No reclaim confirmed
- No retest held
- R:R below minimum
- BTC aggressively selling
- Price far extended from entry
- FOMO or chasing entries

TRADE MANAGEMENT:
- TP1: Take 30% -- move SL to breakeven IMMEDIATELY after TP1
- TP2: Take 40%
- TP3: Remaining position

RESPONSE FORMAT (use exactly this, no changes):

ESS FLOKI 8X SETUP
Setup Type: [A / A+ / NONE]
Status: [WAITING / TRIGGERED / NO TRADE]
BTC Filter: [GO / NO GO -- one sentence]
Sequence: [which of the 5 steps are confirmed]

ENTRY: [exact price or Not triggered]
STOP LOSS: [exact price or Not set]
TP1 (30%): [exact price or Not set]
TP2 (40%): [exact price or Not set]
TP3 (rem): [exact price or Not set]
R:R: [ratio or N/A]

Session: [London / NY / Off-hours]
Action: [ENTER NOW / WAIT / NO TRADE]"""


# ================================================================
# LBANK FUNCTIONS
# ================================================================
def lbank_sign(params):
    sorted_params = "&".join(
        f"{k}={v}" for k, v in sorted(params.items())
    )
    return hmac.new(
        LBANK_SECRET_KEY.encode("utf-8"),
        sorted_params.encode("utf-8"),
        hashlib.sha256
    ).hexdigest().upper()

def lbank_ts():
    return str(int(time.time() * 1000))

def get_price(symbol):
    try:
        r = requests.get(
            f"{LBANK_BASE}/v2/ticker.do",
            params={"symbol": symbol},
            timeout=10
        )
        return float(r.json()["data"][0]["ticker"]["latest"])
    except Exception as e:
        print(f"Price error: {e}")
        return 0.0

def get_balance(asset="usdt"):
    try:
        params = {"api_key": LBANK_API_KEY, "timestamp": lbank_ts()}
        params["sign"] = lbank_sign(params)
        r = requests.post(
            f"{LBANK_BASE}/v2/user_info.do",
            data=params,
            timeout=10
        )
        funds = r.json().get("data", {}).get("info", {}).get("free", {})
        return float(funds.get(asset, 0))
    except Exception as e:
        print(f"Balance error: {e}")
        return 0.0

def place_order(symbol, side, quantity):
    try:
        params = {
            "api_key":   LBANK_API_KEY,
            "symbol":    symbol,
            "type":      side,
            "price":     "0",
            "amount":    str(quantity),
            "timestamp": lbank_ts(),
        }
        params["sign"] = lbank_sign(params)
        r = requests.post(
            f"{LBANK_BASE}/v2/create_order.do",
            data=params,
            timeout=10
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def calc_qty(symbol, usdt_amount):
    price = get_price(symbol)
    if price <= 0:
        return 0.0
    return round(usdt_amount / price, 0)


# ================================================================
# CLAUDE ESS ANALYSIS
# ================================================================
def ask_claude(alert_data):
    headers = {
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    user_msg = (
        f"ESS Alert for {alert_data.get('ticker', 'FLOKI')}:\n"
        f"Price: {alert_data.get('close')}\n"
        f"Signal: {alert_data.get('signal')}\n"
        f"Timeframe: {alert_data.get('timeframe')}m\n"
        f"ESS Step: {alert_data.get('ess_step', 'Not specified')}\n"
        f"BTC Status: {alert_data.get('btc_status', 'Not specified')}\n"
        f"Sequence: {alert_data.get('sequence', 'Not specified')}\n"
        f"Session: {alert_data.get('session', 'Not specified')}\n\n"
        f"Evaluate against ESS FLOKI 8X rules. If the full 5-step "
        f"sequence is not confirmed output STATUS = WAITING and do "
        f"not trigger an entry."
    )
    body = {
        "model":      "claude-sonnet-4-6",
        "max_tokens": 400,
        "system":     ESS_SYSTEM,
        "messages":   [{"role": "user", "content": user_msg}]
    }
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=body,
        timeout=30
    )
    response = r.json()
    print(f"Claude raw response: {response}")

    if "content" not in response:
        error_detail = response.get("error", {}).get("message", str(response))
        raise Exception(f"Claude API error: {error_detail}")

    return response["content"][0]["text"]

def parse_action(analysis):
    for line in analysis.splitlines():
        if line.strip().startswith("Action:"):
            return line.split(":", 1)[1].strip()
    return "WAIT"

def parse_setup_type(analysis):
    for line in analysis.splitlines():
        if "Setup Type:" in line:
            val = line.split(":", 1)[1].strip()
            if "A+" in val:
                return "A+"
            if val == "A":
                return "A"
    return "NONE"


# ================================================================
# TELEGRAM -- plain text only, no Markdown formatting
# ================================================================
def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID",   "")

    if not token or not chat:
        print("Telegram: missing token or chat ID in environment variables")
        return

    try:
        # Remove any characters that could break Telegram delivery
        clean = (message
                 .replace("`", "'")
                 .replace("*", "")
                 .replace("_", " ")
                 .replace("[", "(")
                 .replace("]", ")")
                 .replace("#", ""))

        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat,
                "text":    clean
            },
            timeout=10
        )
        result = r.json()
        if not result.get("ok"):
            print(f"Telegram failed: {result}")
        else:
            print("Telegram sent OK")

    except Exception as e:
        print(f"Telegram error: {e}")


# ================================================================
# ROUTES
# ================================================================
@app.route("/")
def home():
    return "ESS FLOKI 8X -- Claude + LBank Active", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data       = request.json or {}
        ticker     = data.get("ticker", "FLOKIUSDT")
        signal     = data.get("signal", "Unknown")
        price      = data.get("close",  "N/A")

        print(f"--------------------------------------------------")
        print(f"Alert received: {signal} on {ticker} @ {price}")

        # Step 1: Ask Claude to evaluate against ESS rules
        analysis   = ask_claude(data)
        action     = parse_action(analysis)
        setup_type = parse_setup_type(analysis)

        print(f"Claude action: {action}")
        print(f"Setup type:    {setup_type}")
        print(f"Analysis:\n{analysis}")

        # Step 2: Execute only if Claude says ENTER NOW
        order_msg = ""

        if action == "ENTER NOW" and setup_type in ("A", "A+") and LBANK_API_KEY:
            symbol    = "floki_usdt"
            usdt_size = TRADE_SIZE_AP if setup_type == "A+" else TRADE_SIZE_A
            balance   = get_balance("usdt")

            if balance < usdt_size:
                order_msg = f"Insufficient balance: {balance:.2f} USDT (need {usdt_size})"
                print(order_msg)
            else:
                qty = calc_qty(symbol, usdt_size)
                if qty > 0:
                    result = place_order(symbol, "buy", qty)
                    if result.get("result") == "true":
                        oid = result.get("data", {}).get("orderId", "N/A")
                        order_msg = (
                            f"ORDER PLACED -- {setup_type}\n"
                            f"{usdt_size} USDT | {qty} FLOKI | Order ID: {oid}"
                        )
                    else:
                        order_msg = f"Order FAILED: {json.dumps(result)}"
                    print(order_msg)
                else:
                    order_msg = "Could not calculate FLOKI quantity"

        elif action == "ENTER NOW" and not LBANK_API_KEY:
            order_msg = "LBank keys not configured -- analysis only mode"

        # Step 3: Build plain text Telegram message
        if setup_type == "A+":
            badge = "[ESS A+]"
        elif setup_type == "A":
            badge = "[ESS A]"
        else:
            badge = "[ESS]"

        tg_message = (
            f"{badge} {signal} -- {ticker}\n"
            f"Price: {price}\n"
            f"Action: {action}\n"
            f"--------------------------------------------------\n"
            f"{analysis}"
        )

        if order_msg:
            tg_message += f"\n--------------------------------------------------\n{order_msg}"

        send_telegram(tg_message)

        return json.dumps({
            "status":     "ok",
            "action":     action,
            "setup_type": setup_type,
            "order":      order_msg or "no order placed"
        }), 200

    except Exception as e:
        error_msg = f"Webhook error: {str(e)}"
        print(error_msg)
        send_telegram(f"ERROR: {str(e)}")
        return json.dumps({"status": "error", "message": str(e)}), 500


# ================================================================
# START
# ================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
