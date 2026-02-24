"""
ETH Signals Bot - v4 Kraken
Usa Kraken public API (sin key, sin restricciones geograficas).
Corre cada 5 minutos, calcula indicadores tecnicos sobre ETH/USD
y guarda en Supabase cuando hay senal. Email opcional.
"""

import os
import time
import logging
import requests
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
SUPA_URL     = os.environ["SUPABASE_URL"]
SUPA_KEY     = os.environ["SUPABASE_KEY"]
GMAIL_USER   = os.getenv("GMAIL_USER", "")
GMAIL_PASS   = os.getenv("GMAIL_APP_PASS", "")
MAIL_TO      = os.getenv("MAIL_TO", "")
MAIL_ENABLED = bool(GMAIL_USER and GMAIL_PASS and MAIL_TO)

CHECK_EVERY  = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
MIN_SCORE    = int(os.getenv("MIN_SCORE", "4"))
COOLDOWN_H   = int(os.getenv("COOLDOWN_HOURS", "1"))

KRAKEN       = "https://api.kraken.com/0/public/OHLC"
PAIR         = "ETHUSD"
# Kraken intervals in minutes: 15, 60, 240
INTERVALS    = {"15m": 15, "1h": 60, "4h": 240}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

last_alert = {"clase": None, "ts": 0}


# ─────────────────────────────────────────
# MATH
# ─────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:  gains  += d
        else:      losses -= d
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0)) / period
        al = (al * (period - 1) + (-d if d < 0 else 0)) / period
    return 100.0 if al == 0 else 100.0 - (100.0 / (1.0 + ag / al))


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def calc_macd(closes):
    if len(closes) < 35:
        return None
    arr = []
    for i in range(26, len(closes) + 1):
        e12 = calc_ema(closes[:i], 12)
        e26 = calc_ema(closes[:i], 26)
        if e12 and e26:
            arr.append(e12 - e26)
    if len(arr) < 9:
        return None
    sig = calc_ema(arr, 9)
    val = arr[-1]
    return {"macd": val, "signal": sig, "hist": val - (sig or 0)}


def calc_bollinger(closes, period=20, mult=2):
    if len(closes) < period:
        return None
    sl  = closes[-period:]
    avg = sum(sl) / period
    sd  = (sum((x - avg) ** 2 for x in sl) / period) ** 0.5
    return {"upper": avg + mult * sd, "mid": avg, "lower": avg - mult * sd}


# ─────────────────────────────────────────
# ANOMALY DETECTORS
# ─────────────────────────────────────────
def detect_spike(candles):
    if len(candles) < 20:
        return None
    changes = [abs((c["close"] - c["open"]) / max(c["open"], 0.01) * 100)
               for c in candles[-20:-1]]
    avg  = sum(changes) / len(changes) if changes else 0.01
    last = (candles[-1]["close"] - candles[-1]["open"]) / max(candles[-1]["open"], 0.01) * 100
    ratio = abs(last) / max(avg, 0.01)
    return {"last": last, "ratio": ratio, "is_anomaly": ratio > 3, "is_large": abs(last) > 2}


def detect_volume(candles):
    if len(candles) < 21:
        return None
    avg   = sum(c["volume"] for c in candles[-21:-1]) / 20
    last  = candles[-1]["volume"]
    ratio = last / max(avg, 1)
    return {"ratio": ratio, "is_anomaly": ratio > 2.5, "is_extreme": ratio > 5}


def detect_24h(candles_1h):
    if len(candles_1h) < 25:
        return None
    old    = candles_1h[-25]["close"]
    now    = candles_1h[-1]["close"]
    change = (now - old) / max(old, 0.01) * 100
    return {"change": change, "is_unusual": abs(change) > 5, "is_extreme": abs(change) > 10}


def detect_volatility(candles):
    if len(candles) < 21:
        return None
    trs   = [c["high"] - c["low"] for c in candles[-21:]]
    avg   = sum(trs[:-1]) / 20
    ratio = trs[-1] / max(avg, 0.01)
    return {"ratio": ratio, "is_spike": ratio > 2}


# ─────────────────────────────────────────
# FETCH FROM KRAKEN
# ─────────────────────────────────────────
def fetch_candles(interval_key, limit=150):
    """
    Kraken OHLC: returns [time, open, high, low, close, vwap, volume, count]
    interval in minutes: 15, 60, 240
    """
    interval = INTERVALS[interval_key]
    url = f"{KRAKEN}?pair={PAIR}&interval={interval}"
    r   = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()

    if data.get("error"):
        raise Exception(f"Kraken error: {data['error']}")

    # Key is usually "XETHUSD" or "ETHUSD"
    result = data.get("result", {})
    key    = next((k for k in result if k != "last"), None)
    if not key:
        raise Exception("Kraken: no data in response")

    raw = result[key]
    # Kraken returns oldest first, last entry is usually incomplete — skip it
    candles = []
    for c in raw[:-1]:
        candles.append({
            "ts":     int(c[0]),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[6]),
        })
    return candles[-limit:]


# ─────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────
def score_timeframe(candles):
    if not candles:
        return {"score": 0, "label": "—", "cls": "neutral",
                "rsi": None, "macd": None, "bb": None,
                "ema20": None, "ema50": None, "ema200": None, "price": None}

    closes = [c["close"] for c in candles]
    price  = closes[-1]
    rsi    = calc_rsi(closes)
    macd   = calc_macd(closes)
    bb     = calc_bollinger(closes)
    ema20  = calc_ema(closes, 20)
    ema50  = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    score  = 0

    if rsi is not None:
        if   rsi < 25:  score += 2
        elif rsi < 35:  score += 1
        elif rsi > 75:  score -= 2
        elif rsi > 65:  score -= 1

    if macd:
        if   macd["hist"] > 0 and macd["macd"] > 0:  score += 2
        elif macd["hist"] > 0:                         score += 1
        elif macd["hist"] < 0 and macd["macd"] < 0:   score -= 2
        else:                                           score -= 1

    if ema20 and ema50:
        diff = (ema20 - ema50) / ema50 * 100
        if   ema20 > ema50 and diff >  0.5:  score += 2
        elif ema20 > ema50:                   score += 1
        elif ema20 < ema50 and diff < -0.5:  score -= 2
        else:                                 score -= 1

    if bb:
        if   price <= bb["lower"]:  score += 1
        elif price >= bb["upper"]:  score -= 1

    if ema200:
        if   price > ema200 * 1.01:   score += 1
        elif price < ema200 * 0.99:   score -= 1

    label = "LONG"    if score >= 3  else "SHORT"   if score <= -3  else "NEUTRAL"
    cls   = "long"    if score >= 3  else "short"   if score <= -3  else "neutral"

    return {"score": score, "label": label, "cls": cls,
            "rsi": rsi, "macd": macd, "bb": bb,
            "ema20": ema20, "ema50": ema50, "ema200": ema200, "price": price}


# ─────────────────────────────────────────
# MAIN ANALYSIS
# ─────────────────────────────────────────
def analyze():
    log.info("Fetching candles from Kraken...")

    c15m = fetch_candles("15m", 150)
    c1h  = fetch_candles("1h",  150)
    c4h  = fetch_candles("4h",  100)

    if not c1h:
        raise Exception("No se obtuvieron datos de Kraken")

    r15 = score_timeframe(c15m)
    r1h = score_timeframe(c1h)
    r4h = score_timeframe(c4h)

    spike15 = detect_spike(c15m)
    spike1h = detect_spike(c1h)
    vol1h   = detect_volume(c1h)
    ch24    = detect_24h(c1h)
    volat   = detect_volatility(c1h)

    total = r1h["score"]

    # Volume anomaly ±2
    if vol1h and vol1h["is_anomaly"]:
        direction = (1  if spike1h and spike1h["last"] < 0
                     else -1 if spike1h and spike1h["last"] > 0
                     else 0)
        total += direction * (2 if vol1h["is_extreme"] else 1)

    # Price spike ±2
    if spike1h and spike1h["is_large"]:
        if   spike1h["last"] < -3:  total += 2
        elif spike1h["last"] >  3:  total -= 2
        elif spike1h["last"] < -2:  total += 1
        elif spike1h["last"] >  2:  total -= 1

    # 4H alignment ±1
    if r4h["cls"] == r1h["cls"] and r1h["cls"] != "neutral":
        total += 1 if r1h["cls"] == "long" else -1

    anom_fired = (
        (vol1h   and vol1h["is_anomaly"])   or
        (spike1h and spike1h["is_large"])   or
        (ch24    and ch24["is_unusual"])
    )

    max_score = 11

    if   total >=  7:          label, clase = "LONG FUERTE",   "long"
    elif total >=  MIN_SCORE:  label, clase = "SENAL LONG",    "long"
    elif total <= -7:          label, clase = "SHORT FUERTE",  "short"
    elif total <= -MIN_SCORE:  label, clase = "SENAL SHORT",   "short"
    elif anom_fired:           label, clase = "ANOMALIA",      "warning"
    else:                      label, clase = "SIN SENAL",     "neutral"

    price   = r1h["price"]
    rsi_str = f"{r1h['rsi']:.1f}" if r1h["rsi"] else "—"
    log.info(f"Score: {total:+d}/{max_score}  |  {label}  |  ETH ${price:.2f}  |  RSI {rsi_str}")

    return {
        "total": total, "max_score": max_score,
        "label": label, "clase": clase, "price": price,
        "r15": r15, "r1h": r1h, "r4h": r4h,
        "spike15": spike15, "spike1h": spike1h,
        "vol1h": vol1h, "ch24": ch24, "volat": volat,
        "anom_fired": anom_fired,
    }


# ─────────────────────────────────────────
# SUPABASE
# ─────────────────────────────────────────
def save_alert(data):
    r1h = data["r1h"]
    r15 = data["r15"]
    r4h = data["r4h"]

    tags = []
    if r15["cls"] == r1h["cls"] and r1h["cls"] != "neutral":
        tags.append("15M ALINEADO")
    if r4h["cls"] == r1h["cls"] and r1h["cls"] != "neutral":
        tags.append("4H ALINEADO")
    if data["vol1h"] and data["vol1h"]["is_anomaly"]:
        tags.append(f"VOL x{data['vol1h']['ratio']:.1f}")
    if data["spike1h"] and data["spike1h"]["is_large"]:
        s = data["spike1h"]["last"]
        tags.append(f"SPIKE {'+' if s > 0 else ''}{s:.1f}%")
    if data["ch24"] and data["ch24"]["is_unusual"]:
        tags.append(f"24H {'+' if data['ch24']['change'] > 0 else ''}{data['ch24']['change']:.1f}%")

    payload = {
        "crypto":       "ETH",
        "fecha":        datetime.now(timezone.utc).isoformat(),
        "precio":       round(data["price"], 4),
        "score":        data["total"],
        "score_max":    data["max_score"],
        "senal":        data["label"],
        "clase":        data["clase"],
        "rsi":          round(r1h["rsi"], 2)          if r1h["rsi"]    else None,
        "macd":         round(r1h["macd"]["macd"], 4) if r1h["macd"]   else None,
        "macd_hist":    round(r1h["macd"]["hist"], 4) if r1h["macd"]   else None,
        "ema20":        round(r1h["ema20"], 4)         if r1h["ema20"]  else None,
        "ema50":        round(r1h["ema50"], 4)         if r1h["ema50"]  else None,
        "ema200":       round(r1h["ema200"], 4)        if r1h["ema200"] else None,
        "bb_upper":     round(r1h["bb"]["upper"], 4)  if r1h["bb"]     else None,
        "bb_lower":     round(r1h["bb"]["lower"], 4)  if r1h["bb"]     else None,
        "funding":      None,
        "tf_15m_score": r15["score"],  "tf_15m_senal": r15["label"],
        "tf_1h_score":  r1h["score"],  "tf_1h_senal":  r1h["label"],
        "tf_4h_score":  r4h["score"],  "tf_4h_senal":  r4h["label"],
        "spike_pct":    round(data["spike1h"]["last"], 4)  if data["spike1h"] else None,
        "vol_ratio":    round(data["vol1h"]["ratio"], 4)   if data["vol1h"]   else None,
        "change_24h":   round(data["ch24"]["change"], 4)   if data["ch24"]    else None,
        "volat_ratio":  round(data["volat"]["ratio"], 4)   if data["volat"]   else None,
        "tags":         tags,
        "ts":           int(time.time() * 1000),
    }

    headers = {
        "Content-Type":  "application/json",
        "apikey":        SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Prefer":        "return=minimal",
    }

    try:
        r = requests.post(f"{SUPA_URL}/rest/v1/alertas",
                          headers=headers, json=payload, timeout=10)
        if r.ok:
            log.info("Alerta guardada en Supabase OK")
        else:
            log.warning(f"Supabase {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Error Supabase: {e}")

    return tags


# ─────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────
def send_email(data, tags):
    r1h   = data["r1h"]
    total = data["total"]
    label = data["label"]
    price = data["price"]
    clase = data["clase"]
    ch24  = data["ch24"]
    vol1h = data["vol1h"]

    color = "#00e676" if clase == "long" else "#ff4444" if clase == "short" else "#ff9800"
    emoji = "🚀" if total >= 7 else "📈" if clase == "long" else "🔻" if total <= -7 else "📉" if clase == "short" else "⚠️"

    if   total >= 7:       desc = "Todos los indicadores alineados alcistas. Alta confianza."
    elif clase == "long":  desc = "Sesgo alcista. Confirma con 4H antes de entrar."
    elif total <= -7:      desc = "Todos los indicadores bajistas. Alta confianza."
    elif clase == "short": desc = "Sesgo bajista. Confirma en 4H antes de shortear."
    else:                  desc = "Movimiento inusual. Espera confirmacion tecnica."

    rsi_val  = f"{r1h['rsi']:.1f}" if r1h["rsi"] else "—"
    rsi_col  = "#00e676" if r1h["rsi"] and r1h["rsi"] < 35 else "#ff4444" if r1h["rsi"] and r1h["rsi"] > 65 else "#7a9ab5"
    macd_val = f"{r1h['macd']['macd']:+.2f}" if r1h["macd"] else "—"
    macd_col = "#00e676" if r1h["macd"] and r1h["macd"]["hist"] > 0 else "#ff4444"
    ema_val  = "EMA20 > EMA50" if r1h["ema20"] and r1h["ema50"] and r1h["ema20"] > r1h["ema50"] else "EMA20 < EMA50"
    ema_col  = "#00e676" if r1h["ema20"] and r1h["ema50"] and r1h["ema20"] > r1h["ema50"] else "#ff4444"
    vol_val  = f"x{vol1h['ratio']:.2f}" if vol1h else "—"
    vol_col  = "#ff9800" if vol1h and vol1h["is_anomaly"] else "#7a9ab5"
    ch24_val = f"{ch24['change']:+.2f}%" if ch24 else "—"
    ch24_col = "#00e676" if ch24 and ch24["change"] > 0 else "#ff4444"

    def tf_row(tf, name):
        c = "#00e676" if tf["cls"] == "long" else "#ff4444" if tf["cls"] == "short" else "#7a9ab5"
        return (f'<tr><td style="padding:6px 0;font-family:monospace;font-size:10px;color:#3d5a73">{name}</td>'
                f'<td style="padding:6px 0;font-family:monospace;font-size:11px;color:{c};text-align:right">'
                f'{tf["label"]} ({tf["score"]:+d})</td></tr>')

    tags_html = "".join(
        f'<span style="background:#1a2a3a;border:1px solid #2a4a6a;color:#7ac4d4;'
        f'font-family:monospace;font-size:11px;padding:3px 9px;border-radius:12px;'
        f'margin:2px;display:inline-block">{t}</span>'
        for t in tags
    )

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="background:#080c10;color:#e0eaf5;font-family:sans-serif;padding:0;margin:0;">
<div style="max-width:580px;margin:0 auto;padding:20px 16px;">
  <div style="background:#0d1318;border:1px solid #1e2d3d;border-radius:12px;padding:20px 24px;margin-bottom:14px;">
    <div style="font-family:monospace;font-size:9px;color:#3d5a73;letter-spacing:2px;margin-bottom:8px;">CRYPTOBOOK · ETH/USD · KRAKEN</div>
    <div style="font-size:24px;font-weight:900;color:{color};margin-bottom:6px;">{emoji} {label}</div>
    <div style="font-family:monospace;font-size:22px;font-weight:700;color:#00e5ff;margin-bottom:8px;">${price:,.2f} USD</div>
    <div style="font-family:monospace;font-size:10px;color:#7a9ab5;line-height:1.6;">{desc}</div>
  </div>
  <div style="background:#0d1318;border:1px solid #1e2d3d;border-left:4px solid {color};border-radius:12px;padding:16px 20px;margin-bottom:14px;">
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="vertical-align:top;">
          <div style="font-family:monospace;font-size:9px;color:#3d5a73;letter-spacing:2px;margin-bottom:4px;">SCORE</div>
          <div style="font-family:monospace;font-size:40px;font-weight:700;color:{color};line-height:1;">{total:+d}</div>
          <div style="font-family:monospace;font-size:9px;color:#3d5a73;">de {data['max_score']}</div>
        </td>
        <td style="text-align:right;vertical-align:top;">
          <table style="border-collapse:collapse;">
            {tf_row(data['r15'], '15M')}
            {tf_row(data['r1h'], '1H')}
            {tf_row(data['r4h'], '4H')}
          </table>
        </td>
      </tr>
    </table>
  </div>
  <div style="background:#0d1318;border:1px solid #1e2d3d;border-radius:12px;padding:16px 20px;margin-bottom:14px;">
    <div style="font-family:monospace;font-size:9px;color:#3d5a73;letter-spacing:2px;margin-bottom:12px;">INDICADORES</div>
    <table style="width:100%;border-collapse:collapse;">
      <tr style="border-bottom:1px solid #1e2d3d;"><td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">RSI 14</td><td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{rsi_col};text-align:right;">{rsi_val}</td></tr>
      <tr style="border-bottom:1px solid #1e2d3d;"><td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">MACD</td><td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{macd_col};text-align:right;">{macd_val}</td></tr>
      <tr style="border-bottom:1px solid #1e2d3d;"><td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">EMA 20/50</td><td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{ema_col};text-align:right;">{ema_val}</td></tr>
      <tr style="border-bottom:1px solid #1e2d3d;"><td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">VOLUMEN</td><td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{vol_col};text-align:right;">{vol_val}</td></tr>
      <tr><td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">VARIACION 24H</td><td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{ch24_col};text-align:right;">{ch24_val}</td></tr>
    </table>
  </div>
  {'<div style="background:#0d1318;border:1px solid #1e2d3d;border-radius:12px;padding:14px 20px;margin-bottom:14px;">' + tags_html + '</div>' if tags_html else ''}
  <div style="font-family:monospace;font-size:9px;color:#3d5a73;line-height:1.7;border-top:1px solid #1e2d3d;padding-top:12px;">
    {datetime.now().strftime('%d/%m/%Y %H:%M')} | CryptoBook ETH Bot | Kraken<br>
    Sin garantias. Siempre usa stop loss.
  </div>
</div></body></html>"""

    subject = f"{emoji} ETH {label} | Score {total:+d} | ${price:,.0f}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = MAIL_TO
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, MAIL_TO, msg.as_string())
        log.info(f"Email enviado a {MAIL_TO}")
    except Exception as e:
        log.error(f"Error email: {e}")


# ─────────────────────────────────────────
# COOLDOWN
# ─────────────────────────────────────────
def should_alert(clase):
    if clase == "neutral":
        return False
    now      = time.time()
    cooldown = COOLDOWN_H * 3600
    if last_alert["clase"] == clase and (now - last_alert["ts"]) < cooldown:
        mins = int((cooldown - (now - last_alert["ts"])) / 60)
        log.info(f"Cooldown activo '{clase}' — {mins} min restantes")
        return False
    return True


# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main():
    log.info("=" * 52)
    log.info("  ETH Signals Bot  —  Kraken edition")
    log.info(f"  Intervalo: {CHECK_EVERY}s  |  Min score: +/-{MIN_SCORE}  |  Cooldown: {COOLDOWN_H}h")
    log.info(f"  Email: {'ACTIVADO -> ' + MAIL_TO if MAIL_ENABLED else 'DESACTIVADO (solo Supabase)'}")
    log.info("=" * 52)

    while True:
        try:
            data  = analyze()
            clase = data["clase"]

            if should_alert(clase):
                log.info(f"-> Alerta: {data['label']}")
                tags = save_alert(data)
                if MAIL_ENABLED:
                    send_email(data, tags)
                else:
                    log.info("Email desactivado — guardado solo en Supabase")
                last_alert["clase"] = clase
                last_alert["ts"]    = time.time()
            else:
                if clase != "neutral":
                    log.info("Sin alerta (cooldown activo)")

        except requests.exceptions.RequestException as e:
            log.error(f"Error de red: {e}")
        except Exception as e:
            log.exception(f"Error inesperado: {e}")

        log.info(f"Esperando {CHECK_EVERY}s...\n")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
