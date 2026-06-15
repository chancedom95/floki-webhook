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
# POSITION TRACKER
# ================================================================
position = {
    "active":             False,
    "symbol":             "floki_usdt",
    "quantity_original":  0.0,
    "quantity_remaining": 0.0,
    "entry_price":        0.0,
    "sl_price":           0.0,
    "tp1_price":          0.0,
    "tp2_price":          0.0,
    "tp3_price":          0.0,
    "tp1_hit":            False,
    "tp2_hit":            False,
    "tp3_hit":            False,
    "order_id":           None,
    "setup_type":         None,
}

# ================================================================
# ESS SYSTEM PROMPT
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
          Valid in ALL sessions including Asia and off-hours.
Setup A+: BTC confirmed recovery + expanding momentum. Full position.
          Min R:R 1:4. Preferred 1:6 to 1:8+.
          Valid in ALL active sessions (Asia, London, NY, Overlap).
          Off-hours only qualifies as Setup A regardless of conditions.

SESSION QUALITY ASSESSMENT (factor into analysis, not a blocker):
- London/NY Overlap (13:00-16:00 UTC): Highest liquidity. Best execution.
- London (07:00-16:00 UTC): High liquidity. Strong for entries.
- New York (12:00-21:00 UTC): High liquidity. Strong for entries.
- Asia (00:00-08:00 UTC): Valid session. Lower liquidity but genuine
  setups occur. Require BTC to be clearly confirming -- no ambiguity.
- Off-hours: Lower quality. Cap at Setup A. Reduce position size.

NO TRADE CONDITIONS (output STATUS = NO TRADE if ANY apply):
- No BTC confirmation
- No reclaim confirmed
- No retest held
- R:R below minimum for setup type
- BTC aggressively selling
- Price far extended from planned entry zone
- FOMO or chasing entries

TRADE MANAGEMENT:
- TP1: Take 30% -- move SL to breakeven IMMEDIATELY
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

Session: [session name and quality assessment]
Action: [ENTER NOW / WAIT / NO TRADE]"""


# ================================================================
# LBANK
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

def get_floki_balance():
    try:
        params = {"api_key": LBANK_API_KEY, "timestamp": lbank_ts()}
        params["sign"] = lbank_sign(params)
        r = requests.post(
            f"{LBANK_BASE}/v2/user_info.do",
            data=params, timeout=10
        )
        funds = r.json().get("data", {}).get("info", {}).get("free", {})
        return float(funds.get("floki", 0))
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
            "amount":    str(round(quantity, 0)),
            "timestamp": lbank_ts(),
        }
        params["sign"] = lbank_sign(params)
        r = requests.post(
            f"{LBANK_BASE}/v2/create_order.do",
            data=params, timeout=10
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def calc_qty(symbol, usdt_amount):
    price = get_price(symbol)
    if price <= 0:
        return 0.0
    return round(usdt_amount / price, 0)


def get_current_session():
    """Determine actual trading session from UTC time -- more reliable than alert message value"""
    import datetime
    h = datetime.datetime.utcnow().hour

    asia_act   = 0 <= h < 8
    london_act = 7 <= h < 16
    ny_act     = 12 <= h < 21
    overlap    = london_act and ny_act

    if overlap:
        return "London/NY Overlap (13:00-16:00 UTC) -- Best liquidity"
    elif london_act:
        return "London (07:00-16:00 UTC) -- High liquidity"
    elif ny_act:
        return "New York (12:00-21:00 UTC) -- High liquidity"
    elif asia_act:
        return "Asia (00:00-08:00 UTC) -- Valid session, lower liquidity"
    else:
        return "Off-Hours -- Lower quality, Setup A only"

# ================================================================
# CLAUDE
# ================================================================
def ask_claude(alert_data):
    headers = {
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    # Session determined server-side from actual UTC time for accuracy
    session = get_current_session()
    user_msg = (
        f"ESS Alert for {alert_data.get('ticker', 'FLOKI')}:\n"
        f"Price: {alert_data.get('close')}\n"
        f"Signal: {alert_data.get('signal')}\n"
        f"Timeframe: {alert_data.get('timeframe')}m\n"
        f"ESS Step: {alert_data.get('ess_step', 'Not specified')}\n"
        f"BTC Status: {alert_data.get('btc_status', 'Not specified')}\n"
        f"Sequence: {alert_data.get('sequence', 'Not specified')}\n"
        f"Session: {session}\n\n"
        f"Evaluate against ESS FLOKI 8X rules. If full 5-step "
        f"sequence is not confirmed output STATUS = WAITING."
    )
    body = {
        "model":      "claude-sonnet-4-6",
        "max_tokens": 400,
        "system":     ESS_SYSTEM,
        "messages":   [{"role": "user", "content": user_msg}]
    }
    r        = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers, json=body, timeout=30
    )
    response = r.json()
    print(f"Claude: {response}")
    if "content" not in response:
        error = response.get("error", {}).get("message", str(response))
        raise Exception(f"Claude API error: {error}")
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
            if "A+" in val: return "A+"
            if val == "A":  return "A"
    return "NONE"


# ================================================================
# TELEGRAM
# ================================================================
def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID",   "")
    if not token or not chat:
        return
    try:
        clean = (message
                 .replace("`","'").replace("*","")
                 .replace("_"," ").replace("[","(")
                 .replace("]",")").replace("#",""))
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": clean},
            timeout=10
        )
        if not r.json().get("ok"):
            print(f"Telegram failed: {r.json()}")
        else:
            print("Telegram sent OK")
    except Exception as e:
        print(f"Telegram error: {e}")


# ================================================================
# TRADE HANDLERS
# ================================================================
def handle_entry(data, analysis, setup_type):
    global position
    if position["active"]:
        return "Entry skipped -- position already active"
    if not LBANK_API_KEY:
        return "LBank keys not set -- analysis only"

    symbol    = "floki_usdt"
    usdt_size = TRADE_SIZE_AP if setup_type == "A+" else TRADE_SIZE_A
    qty       = calc_qty(symbol, usdt_size)
    if qty <= 0:
        return "Could not calculate quantity"

    result = place_order(symbol, "buy", qty)
    print(f"Entry result: {result}")

    if result.get("result") == "true":
        oid   = result.get("data", {}).get("orderId", "N/A")
        price = float(data.get("close", 0))
        position.update({
            "active":             True,
            "symbol":             symbol,
            "quantity_original":  qty,
            "quantity_remaining": qty,
            "entry_price":        price,
            "tp1_hit":            False,
            "tp2_hit":            False,
            "tp3_hit":            False,
            "order_id":           oid,
            "setup_type":         setup_type,
        })
        return (
            f"ENTRY PLACED -- {setup_type}\n"
            f"Bought: {qty} FLOKI\n"
            f"Price: {price}\n"
            f"Size: {usdt_size} USDT\n"
            f"Order ID: {oid}\n"
            f"System will auto-execute TP1 30% / TP2 40% / TP3 rem\n"
            f"SL moves to breakeven automatically after TP1"
        )
    else:
        return f"Entry FAILED: {json.dumps(result)}"


def handle_tp1(data):
    global position
    if not position["active"]:
        return "TP1 skipped -- no active position"
    if position["tp1_hit"]:
        return "TP1 already executed"

    qty_sell = round(position["quantity_original"] * 0.30, 0)
    if qty_sell <= 0:
        return "TP1 quantity too small"

    result = place_order(position["symbol"], "sell", qty_sell)
    print(f"TP1 result: {result}")

    if result.get("result") == "true":
        oid = result.get("data", {}).get("orderId", "N/A")
        position["tp1_hit"]            = True
        position["quantity_remaining"] -= qty_sell
        position["sl_price"]           = position["entry_price"]
        return (
            f"TP1 EXECUTED -- 30% CLOSED\n"
            f"Sold: {qty_sell} FLOKI\n"
            f"Remaining: {position['quantity_remaining']} FLOKI\n"
            f"Order ID: {oid}\n"
            f"SL moved to breakeven: {position['entry_price']}\n"
            f"Holding for TP2..."
        )
    else:
        return f"TP1 FAILED: {json.dumps(result)} -- CLOSE 30% MANUALLY NOW"


def handle_tp2(data):
    global position
    if not position["active"]:
        return "TP2 skipped -- no active position"
    if not position["tp1_hit"]:
        return "TP2 skipped -- TP1 not hit yet"
    if position["tp2_hit"]:
        return "TP2 already executed"

    qty_sell = round(position["quantity_original"] * 0.40, 0)
    qty_sell = min(qty_sell, position["quantity_remaining"])
    if qty_sell <= 0:
        return "TP2 quantity too small"

    result = place_order(position["symbol"], "sell", qty_sell)
    print(f"TP2 result: {result}")

    if result.get("result") == "true":
        oid = result.get("data", {}).get("orderId", "N/A")
        position["tp2_hit"]            = True
        position["quantity_remaining"] -= qty_sell
        return (
            f"TP2 EXECUTED -- 40% CLOSED\n"
            f"Sold: {qty_sell} FLOKI\n"
            f"Remaining: {position['quantity_remaining']} FLOKI\n"
            f"Order ID: {oid}\n"
            f"Holding remainder for TP3..."
        )
    else:
        return f"TP2 FAILED: {json.dumps(result)} -- CLOSE 40% MANUALLY NOW"


def handle_tp3(data):
    global position
    if not position["active"]:
        return "TP3 skipped -- no active position"
    if not position["tp2_hit"]:
        return "TP3 skipped -- TP2 not hit yet"
    if position["tp3_hit"]:
        return "TP3 already executed"

    actual = get_floki_balance()
    qty_sell = round(actual if actual > 0 else position["quantity_remaining"], 0)
    if qty_sell <= 0:
        position["active"] = False
        return "TP3: no FLOKI remaining -- already closed"

    result = place_order(position["symbol"], "sell", qty_sell)
    print(f"TP3 result: {result}")

    if result.get("result") == "true":
        oid = result.get("data", {}).get("orderId", "N/A")
        position.update({
            "tp3_hit": True,
            "quantity_remaining": 0,
            "active": False
        })
        return (
            f"TP3 EXECUTED -- TRADE COMPLETE\n"
            f"Sold: {qty_sell} FLOKI\n"
            f"Order ID: {oid}\n"
            f"Full ESS sequence completed\n"
            f"Position closed. Ready for next setup."
        )
    else:
        return f"TP3 FAILED: {json.dumps(result)} -- CLOSE REMAINING MANUALLY NOW"


def handle_sl(data):
    global position
    if not position["active"]:
        return "SL alert -- no active position"

    actual = get_floki_balance()
    qty_sell = round(actual if actual > 0 else position["quantity_remaining"], 0)
    if qty_sell <= 0:
        position["active"] = False
        return "SL: already closed"

    result = place_order(position["symbol"], "sell", qty_sell)
    print(f"SL result: {result}")

    if result.get("result") == "true":
        oid = result.get("data", {}).get("orderId", "N/A")
        was_be = position["tp1_hit"]
        position.update({"active": False, "quantity_remaining": 0})
        return (
            f"STOP LOSS EXECUTED\n"
            f"Sold: {qty_sell} FLOKI\n"
            f"Order ID: {oid}\n"
            f"{'SL was at breakeven -- protected' if was_be else 'Loss taken per ESS rules'}\n"
            f"Position closed."
        )
    else:
        return f"SL FAILED: {json.dumps(result)} -- CLOSE MANUALLY ON LBANK NOW"


def handle_invalidated(data):
    global position
    if not position["active"]:
        return "BTC invalidation -- no active position"

    actual = get_floki_balance()
    qty_sell = round(actual if actual > 0 else position["quantity_remaining"], 0)
    if qty_sell <= 0:
        position["active"] = False
        return "Invalidated: nothing to close"

    result = place_order(position["symbol"], "sell", qty_sell)
    if result.get("result") == "true":
        position.update({"active": False, "quantity_remaining": 0})
        return (
            f"BTC INVALIDATION -- POSITION CLOSED\n"
            f"Sold: {qty_sell} FLOKI\n"
            f"BTC lost key support\n"
            f"Position cleared per ESS rules."
        )
    else:
        return f"Invalidation close FAILED -- CLOSE MANUALLY ON LBANK NOW"


# ================================================================
# WEBHOOK
# ================================================================
@app.route("/")
def home():
    status = "ACTIVE" if position["active"] else "IDLE"
    return f"ESS FLOKI 8X -- Claude + LBank | Position: {status}", 200


@app.route("/status")
def pos_status():
    return json.dumps(position, indent=2), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data   = request.json or {}
        ticker = data.get("ticker", "FLOKIUSDT")
        signal = data.get("signal", "Unknown")
        price  = data.get("close",  "N/A")

        print(f"\n{'='*50}\nSignal: {signal} | {ticker} @ {price}")
        print(f"Position: {position['active']} | Held: {position['quantity_remaining']}")

        result_msg = ""
        analysis   = ""
        action     = ""
        setup_type = ""

        # Route by signal
        if signal in ("ESS A+ SETUP", "ESS A SETUP"):
            analysis   = ask_claude(data)
            action     = parse_action(analysis)
            setup_type = parse_setup_type(analysis)
            if action == "ENTER NOW" and setup_type in ("A", "A+"):
                result_msg = handle_entry(data, analysis, setup_type)
            else:
                result_msg = f"Claude: {action} -- no order"

        elif signal == "ESS TP1 HIT":
            result_msg = handle_tp1(data)

        elif signal == "ESS TP2 HIT":
            result_msg = handle_tp2(data)

        elif signal == "ESS TP3 HIT":
            result_msg = handle_tp3(data)

        elif signal == "ESS STOP LOSS HIT":
            result_msg = handle_sl(data)

        elif signal == "ESS INVALIDATED":
            result_msg = handle_invalidated(data)

        elif signal in ("ESS SWEEP DETECTED", "ESS RECLAIM CONFIRMED", "ESS RETEST HOLD"):
            steps = {
                "ESS SWEEP DETECTED":    "Step 1 done -- sweep confirmed. Watching for reclaim.",
                "ESS RECLAIM CONFIRMED": "Step 2 done -- reclaim confirmed. Watching for retest.",
                "ESS RETEST HOLD":       "Step 3 done -- retest held. Watching for BTC + trigger.",
            }
            result_msg = steps.get(signal, signal)

        else:
            result_msg = f"Signal received: {signal}"

        # Build Telegram message
        badges = {
            "ESS A+ SETUP": "(A+)", "ESS A SETUP": "(A)",
            "ESS TP1 HIT": "(TP1)", "ESS TP2 HIT": "(TP2)",
            "ESS TP3 HIT": "(TP3)", "ESS STOP LOSS HIT": "(SL)",
            "ESS INVALIDATED": "(INV)", "ESS SWEEP DETECTED": "(SW)",
            "ESS RECLAIM CONFIRMED": "(RC)", "ESS RETEST HOLD": "(RT)",
        }
        badge = badges.get(signal, "(i)")
        tg    = f"{badge} {ticker} @ {price}\n{signal}\n\n"
        if analysis:
            tg += f"{analysis}\n\n"
        tg += result_msg

        if position["active"]:
            tg += (
                f"\n\nPosition: {position['quantity_remaining']} FLOKI held"
                f"\nTP1: {'Done' if position['tp1_hit'] else 'Pending'}"
                f" | TP2: {'Done' if position['tp2_hit'] else 'Pending'}"
                f" | TP3: Pending"
            )

        send_telegram(tg)

        return json.dumps({
            "status": "ok", "signal": signal,
            "action": action or "routed", "result": result_msg
        }), 200

    except Exception as e:
        print(f"Error: {e}")
        send_telegram(f"ERROR: {str(e)}")
        return json.dumps({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"ESS FLOKI 8X starting on port {port}")
    app.run(host="0.0.0.0", port=port)
