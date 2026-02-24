"""
ETH Signals Bot
Corre cada 5 minutos, calcula indicadores técnicos sobre ETH/USDT
y manda email + guarda en Supabase cuando hay señal.
"""

import os
import time
import logging
import requests
import smtplib
import json
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ─────────────────────────────────────────
# CONFIG (se carga desde variables de entorno)
# ─────────────────────────────────────────
SUPA_URL    = os.environ["SUPABASE_URL"]
SUPA_KEY    = os.environ["SUPABASE_KEY"]
GMAIL_USER  = os.getenv("GMAIL_USER", "")       # opcional
GMAIL_PASS  = os.getenv("GMAIL_APP_PASS", "")   # opcional
MAIL_TO     = os.getenv("MAIL_TO", "")          # opcional
MAIL_ENABLED = bool(GMAIL_USER and GMAIL_PASS and MAIL_TO)
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "300"))   # 5 min por defecto
MIN_SCORE   = int(os.getenv("MIN_SCORE", "4"))               # umbral para alertar
COOLDOWN_H  = int(os.getenv("COOLDOWN_HOURS", "1"))          # horas entre alertas iguales

BINANCE     = "https://api.binance.com/api/v3"
BINANCE_F   = "https://fapi.binance.com/fapi/v1"
SYMBOL      = "ETHUSDT"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# Evitar spam: guarda la última clase de señal enviada y cuándo
last_alert = {"clase": None, "ts": 0}


# ─────────────────────────────────────────
# MATH HELPERS
# ─────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + (d if d > 0 else 0)) / period
        avg_l = (avg_l * (period - 1) + (-d if d < 0 else 0)) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def calc_macd(closes):
    if len(closes) < 35:
        return None
    macd_arr = []
    for i in range(26, len(closes) + 1):
        e12 = calc_ema(closes[:i], 12)
        e26 = calc_ema(closes[:i], 26)
        if e12 and e26:
            macd_arr.append(e12 - e26)
    if len(macd_arr) < 9:
        return None
    signal = calc_ema(macd_arr, 9)
    macd_val = macd_arr[-1]
    return {"macd": macd_val, "signal": signal, "hist": macd_val - (signal or 0)}


def calc_bollinger(closes, period=20, std_mult=2):
    if len(closes) < period:
        return None
    sl = closes[-period:]
    avg = sum(sl) / period
    variance = sum((x - avg) ** 2 for x in sl) / period
    sd = variance ** 0.5
    return {"upper": avg + std_mult * sd, "mid": avg, "lower": avg - std_mult * sd}


# ─────────────────────────────────────────
# ANOMALY DETECTORS
# ─────────────────────────────────────────
def detect_spike(candles):
    if len(candles) < 50:
        return None
    changes = [abs((c["close"] - c["open"]) / c["open"] * 100) for c in candles[-50:-1]]
    avg = sum(changes) / len(changes) if changes else 0.01
    last_c = candles[-1]
    last = (last_c["close"] - last_c["open"]) / last_c["open"] * 100
    ratio = abs(last) / max(avg, 0.01)
    return {"last": last, "ratio": ratio, "is_anomaly": ratio > 3, "is_large": abs(last) > 2}


def detect_volume(candles):
    if len(candles) < 51:
        return None
    avg = sum(c["volume"] for c in candles[-51:-1]) / 50
    last = candles[-1]["volume"]
    ratio = last / max(avg, 1)
    return {"ratio": ratio, "is_anomaly": ratio > 2.5, "is_extreme": ratio > 5}


def detect_24h(candles_1h):
    if len(candles_1h) < 25:
        return None
    old = candles_1h[-25]["close"]
    now = candles_1h[-1]["close"]
    change = (now - old) / old * 100
    return {"change": change, "is_unusual": abs(change) > 5, "is_extreme": abs(change) > 10}


def detect_volatility(candles):
    if len(candles) < 21:
        return None
    trs = [c["high"] - c["low"] for c in candles[-21:]]
    avg = sum(trs[:-1]) / 20
    ratio = trs[-1] / max(avg, 0.01)
    return {"ratio": ratio, "is_spike": ratio > 2}


# ─────────────────────────────────────────
# FETCH FROM BINANCE
# ─────────────────────────────────────────
def fetch_candles(symbol, interval, limit=150):
    url = f"{BINANCE}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return [
        {
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
        }
        for c in r.json()
    ]


def fetch_funding():
    try:
        url = f"{BINANCE_F}/premiumIndex?symbol={SYMBOL}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return float(r.json()["lastFundingRate"])
    except Exception:
        return None


# ─────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────
def score_timeframe(candles):
    if not candles:
        return {"score": 0, "label": "—", "cls": "neutral", "rsi": None, "macd": None}
    closes = [c["close"] for c in candles]
    price = closes[-1]
    rsi   = calc_rsi(closes)
    macd  = calc_macd(closes)
    bb    = calc_bollinger(closes)
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    ema200= calc_ema(closes, 200)

    score = 0

    # RSI ±2
    if rsi is not None:
        if rsi < 25:   score += 2
        elif rsi < 35: score += 1
        elif rsi > 75: score -= 2
        elif rsi > 65: score -= 1

    # MACD ±2
    if macd:
        if macd["hist"] > 0 and macd["macd"] > 0:   score += 2
        elif macd["hist"] > 0:                        score += 1
        elif macd["hist"] < 0 and macd["macd"] < 0:  score -= 2
        else:                                          score -= 1

    # EMA 20/50 ±2
    if ema20 and ema50:
        diff = (ema20 - ema50) / ema50 * 100
        if ema20 > ema50 and diff > 0.5:   score += 2
        elif ema20 > ema50:                 score += 1
        elif ema20 < ema50 and diff < -0.5: score -= 2
        else:                               score -= 1

    # Bollinger ±1
    if bb:
        if price <= bb["lower"]:  score += 1
        elif price >= bb["upper"]: score -= 1

    # EMA200 ±1
    if ema200:
        if price > ema200 * 1.01:   score += 1
        elif price < ema200 * 0.99: score -= 1

    label = "▲ LONG" if score >= 3 else "▼ SHORT" if score <= -3 else "◆ NEUTRAL"
    cls   = "long"   if score >= 3 else "short"   if score <= -3 else "neutral"

    return {
        "score": score, "label": label, "cls": cls,
        "rsi": rsi, "macd": macd, "bb": bb,
        "ema20": ema20, "ema50": ema50, "ema200": ema200, "price": price
    }


# ─────────────────────────────────────────
# MAIN ANALYSIS
# ─────────────────────────────────────────
def analyze():
    log.info("Fetching candles...")
    c15m = fetch_candles(SYMBOL, "15m", 150)
    c1h  = fetch_candles(SYMBOL, "1h",  150)
    c4h  = fetch_candles(SYMBOL, "4h",  150)
    fund = fetch_funding()

    r15  = score_timeframe(c15m)
    r1h  = score_timeframe(c1h)
    r4h  = score_timeframe(c4h)

    spike15 = detect_spike(c15m)
    spike1h = detect_spike(c1h)
    vol1h   = detect_volume(c1h)
    ch24    = detect_24h(c1h)
    volat   = detect_volatility(c1h)

    total = r1h["score"]

    # Funding ±2
    fund_score = 0
    fund_sig   = "NEUTRAL"
    if fund is not None:
        if fund < -0.01:   fund_score = 2;  fund_sig = "MUY NEGATIVO → LONG"
        elif fund < 0:     fund_score = 1;  fund_sig = "NEGATIVO → LONG"
        elif fund > 0.01:  fund_score = -2; fund_sig = "MUY POSITIVO → SHORT"
        elif fund > 0.005: fund_score = -1; fund_sig = "POSITIVO → SHORT"
        total += fund_score

    # Volume anomaly ±2
    if vol1h and vol1h["is_anomaly"]:
        direction = 1 if (spike1h and spike1h["last"] < 0) else -1 if (spike1h and spike1h["last"] > 0) else 0
        vol_score = direction * (2 if vol1h["is_extreme"] else 1)
        total += vol_score

    # Price spike ±2
    if spike1h and spike1h["is_large"]:
        if spike1h["last"] < -3:   total += 2
        elif spike1h["last"] > 3:  total -= 2
        elif spike1h["last"] < -2: total += 1
        elif spike1h["last"] > 2:  total -= 1

    # 4H alignment ±1
    if r4h["cls"] == r1h["cls"] and r1h["cls"] != "neutral":
        total += 1 if r1h["cls"] == "long" else -1

    # Determine signal
    anom_fired = (
        (vol1h and vol1h["is_anomaly"]) or
        (spike1h and spike1h["is_large"]) or
        (ch24 and ch24["is_unusual"])
    )

    if total >= 7:
        label = "🚀 LONG FUERTE"
        clase = "long"
    elif total >= MIN_SCORE:
        label = "▲ SEÑAL LONG"
        clase = "long"
    elif total <= -7:
        label = "🔻 SHORT FUERTE"
        clase = "short"
    elif total <= -MIN_SCORE:
        label = "▼ SEÑAL SHORT"
        clase = "short"
    elif anom_fired:
        label = "⚠️ MOVIMIENTO ANÓMALO"
        clase = "warning"
    else:
        label = "◆ SIN SEÑAL"
        clase = "neutral"

    price = r1h["price"]
    log.info(f"Score: {total:+d}  |  {label}  |  ETH ${price:.2f}  |  RSI {r1h['rsi']:.1f if r1h['rsi'] else '—'}")

    return {
        "total": total,
        "label": label,
        "clase": clase,
        "price": price,
        "r15": r15, "r1h": r1h, "r4h": r4h,
        "fund": fund, "fund_sig": fund_sig, "fund_score": fund_score,
        "spike15": spike15, "spike1h": spike1h,
        "vol1h": vol1h, "ch24": ch24, "volat": volat,
        "anom_fired": anom_fired,
    }


# ─────────────────────────────────────────
# SUPABASE SAVE
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
    if data["fund"] is not None and abs(data["fund"]) > 0.005:
        tags.append(f"FUND {data['fund'] * 100:.3f}%")
    if data["ch24"] and data["ch24"]["is_unusual"]:
        tags.append(f"24H {'+' if data['ch24']['change'] > 0 else ''}{data['ch24']['change']:.1f}%")

    payload = {
        "crypto":        "ETH",
        "fecha":         datetime.now(timezone.utc).isoformat(),
        "precio":        round(data["price"], 4),
        "score":         data["total"],
        "score_max":     13,
        "senal":         data["label"],
        "clase":         data["clase"],
        "rsi":           round(r1h["rsi"], 2) if r1h["rsi"] else None,
        "macd":          round(r1h["macd"]["macd"], 4) if r1h["macd"] else None,
        "macd_hist":     round(r1h["macd"]["hist"], 4) if r1h["macd"] else None,
        "ema20":         round(r1h["ema20"], 4) if r1h["ema20"] else None,
        "ema50":         round(r1h["ema50"], 4) if r1h["ema50"] else None,
        "ema200":        round(r1h["ema200"], 4) if r1h["ema200"] else None,
        "bb_upper":      round(r1h["bb"]["upper"], 4) if r1h["bb"] else None,
        "bb_lower":      round(r1h["bb"]["lower"], 4) if r1h["bb"] else None,
        "funding":       round(data["fund"], 6) if data["fund"] is not None else None,
        "tf_15m_score":  r15["score"],
        "tf_15m_senal":  r15["label"],
        "tf_1h_score":   r1h["score"],
        "tf_1h_senal":   r1h["label"],
        "tf_4h_score":   r4h["score"],
        "tf_4h_senal":   r4h["label"],
        "spike_pct":     round(data["spike1h"]["last"], 4) if data["spike1h"] else None,
        "vol_ratio":     round(data["vol1h"]["ratio"], 4) if data["vol1h"] else None,
        "change_24h":    round(data["ch24"]["change"], 4) if data["ch24"] else None,
        "volat_ratio":   round(data["volat"]["ratio"], 4) if data["volat"] else None,
        "tags":          tags,
        "ts":            int(time.time() * 1000),
    }

    headers = {
        "Content-Type":  "application/json",
        "apikey":        SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Prefer":        "return=minimal",
    }

    try:
        r = requests.post(
            f"{SUPA_URL}/rest/v1/alertas",
            headers=headers,
            json=payload,
            timeout=10
        )
        if r.ok:
            log.info("✓ Alerta guardada en Supabase")
        else:
            log.warning(f"Supabase error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Error guardando en Supabase: {e}")

    return tags


# ─────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────
def send_email(data, tags):
    r1h    = data["r1h"]
    total  = data["total"]
    label  = data["label"]
    price  = data["price"]
    clase  = data["clase"]
    ch24   = data["ch24"]
    vol1h  = data["vol1h"]
    fund   = data["fund"]

    # Color según señal
    color  = "#00e676" if clase == "long" else "#ff4444" if clase == "short" else "#ff9800"
    emoji  = "🚀" if total >= 7 else "📈" if clase == "long" else "🔻" if total <= -7 else "📉" if clase == "short" else "⚠️"

    # Descripción de la señal
    if total >= 7:
        desc = "Todos los indicadores alineados alcistas. Alta confianza. Buscá entrada con stop bajo soporte."
    elif clase == "long":
        desc = "Sesgo alcista. Confirmá que el 4H también sea LONG. Esperá pullback si podés."
    elif total <= -7:
        desc = "Todos los indicadores bajistas. Alta confianza. Stop sobre resistencia más cercana."
    elif clase == "short":
        desc = "Sesgo bajista. Confirmá en 4H antes de entrar short."
    else:
        desc = "Movimiento inusual detectado. Esperá confirmación técnica antes de operar."

    # Tags HTML
    tags_html = "".join(
        f'<span style="background:#1a2a3a;border:1px solid #2a4a6a;color:#7ac4d4;'
        f'font-family:monospace;font-size:11px;padding:3px 9px;border-radius:12px;'
        f'margin:2px;display:inline-block">{t}</span>'
        for t in tags
    ) if tags else ""

    # Tabla de indicadores
    rsi_val  = f"{r1h['rsi']:.1f}" if r1h["rsi"] else "—"
    rsi_col  = "#00e676" if r1h["rsi"] and r1h["rsi"] < 35 else "#ff4444" if r1h["rsi"] and r1h["rsi"] > 65 else "#7a9ab5"
    macd_val = f"{r1h['macd']['macd']:+.2f}" if r1h["macd"] else "—"
    macd_col = "#00e676" if r1h["macd"] and r1h["macd"]["hist"] > 0 else "#ff4444"
    ema_val  = "EMA20 > EMA50" if r1h["ema20"] and r1h["ema50"] and r1h["ema20"] > r1h["ema50"] else "EMA20 < EMA50"
    ema_col  = "#00e676" if r1h["ema20"] and r1h["ema50"] and r1h["ema20"] > r1h["ema50"] else "#ff4444"
    fund_val = f"{fund * 100:.4f}%" if fund is not None else "—"
    fund_col = "#00e676" if fund and fund < 0 else "#ff4444" if fund and fund > 0 else "#7a9ab5"
    vol_val  = f"x{vol1h['ratio']:.2f}" if vol1h else "—"
    vol_col  = "#ff9800" if vol1h and vol1h["is_anomaly"] else "#7a9ab5"
    ch24_val = f"{ch24['change']:+.2f}%" if ch24 else "—"
    ch24_col = "#00e676" if ch24 and ch24["change"] > 0 else "#ff4444"

    tf_row = lambda tf, name: (
        f'<td style="padding:8px 12px;font-family:monospace;font-size:11px;'
        f'color:{"#00e676" if tf["cls"]=="long" else "#ff4444" if tf["cls"]=="short" else "#7a9ab5"}">'
        f'{name}: {tf["label"]} ({tf["score"]:+d})</td>'
    )

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="background:#080c10;color:#e0eaf5;font-family:sans-serif;padding:0;margin:0;">
<div style="max-width:600px;margin:0 auto;padding:24px 16px;">

  <!-- HEADER -->
  <div style="background:#0d1318;border:1px solid #1e2d3d;border-radius:12px;padding:20px 24px;margin-bottom:16px;">
    <div style="font-family:monospace;font-size:10px;color:#3d5a73;letter-spacing:2px;margin-bottom:6px;">CRYPTOBOOK · ETH/USDT · SEÑAL AUTOMÁTICA</div>
    <div style="font-size:26px;font-weight:900;color:{color};margin-bottom:6px;">{emoji} {label}</div>
    <div style="font-family:monospace;font-size:22px;font-weight:700;color:#00e5ff;margin-bottom:8px;">${price:,.2f} USDT</div>
    <div style="font-family:monospace;font-size:11px;color:#7a9ab5;line-height:1.6;">{desc}</div>
  </div>

  <!-- SCORE -->
  <div style="background:#0d1318;border:1px solid #1e2d3d;border-left:4px solid {color};border-radius:12px;padding:16px 20px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;">
    <div>
      <div style="font-family:monospace;font-size:9px;color:#3d5a73;letter-spacing:2px;margin-bottom:4px;">SCORE TOTAL</div>
      <div style="font-family:monospace;font-size:36px;font-weight:700;color:{color};line-height:1;">{total:+d}</div>
      <div style="font-family:monospace;font-size:9px;color:#3d5a73;">de 13 posibles</div>
    </div>
    <div style="text-align:right;">
      <div style="font-family:monospace;font-size:9px;color:#3d5a73;letter-spacing:2px;margin-bottom:6px;">TIMEFRAMES</div>
      <table style="border-collapse:collapse;">
        <tr>{tf_row(data['r15'], '15M')}</tr>
        <tr>{tf_row(data['r1h'], '1H')}</tr>
        <tr>{tf_row(data['r4h'], '4H')}</tr>
      </table>
    </div>
  </div>

  <!-- INDICADORES -->
  <div style="background:#0d1318;border:1px solid #1e2d3d;border-radius:12px;padding:16px 20px;margin-bottom:16px;">
    <div style="font-family:monospace;font-size:9px;color:#3d5a73;letter-spacing:2px;margin-bottom:12px;">INDICADORES</div>
    <table style="width:100%;border-collapse:collapse;">
      <tr style="border-bottom:1px solid #1e2d3d;">
        <td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">RSI 14</td>
        <td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{rsi_col};text-align:right;">{rsi_val}</td>
      </tr>
      <tr style="border-bottom:1px solid #1e2d3d;">
        <td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">MACD</td>
        <td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{macd_col};text-align:right;">{macd_val}</td>
      </tr>
      <tr style="border-bottom:1px solid #1e2d3d;">
        <td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">EMA 20/50</td>
        <td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{ema_col};text-align:right;">{ema_val}</td>
      </tr>
      <tr style="border-bottom:1px solid #1e2d3d;">
        <td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">FUNDING RATE</td>
        <td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{fund_col};text-align:right;">{fund_val}</td>
      </tr>
      <tr style="border-bottom:1px solid #1e2d3d;">
        <td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">VOLUMEN</td>
        <td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{vol_col};text-align:right;">{vol_val}</td>
      </tr>
      <tr>
        <td style="padding:8px 0;font-family:monospace;font-size:10px;color:#3d5a73;">VARIACIÓN 24H</td>
        <td style="padding:8px 0;font-family:monospace;font-size:12px;font-weight:700;color:{ch24_col};text-align:right;">{ch24_val}</td>
      </tr>
    </table>
  </div>

  <!-- TAGS -->
  {'<div style="background:#0d1318;border:1px solid #1e2d3d;border-radius:12px;padding:14px 20px;margin-bottom:16px;"><div style="font-family:monospace;font-size:9px;color:#3d5a73;letter-spacing:2px;margin-bottom:8px;">SEÑALES ACTIVAS</div>' + tags_html + '</div>' if tags_html else ''}

  <!-- DISCLAIMER -->
  <div style="font-family:monospace;font-size:9px;color:#3d5a73;line-height:1.7;border-top:1px solid #1e2d3d;padding-top:14px;">
    ⚠️ Generado automáticamente por CryptoBook · {datetime.now().strftime('%d/%m/%Y %H:%M')} AR<br>
    Este sistema no garantiza resultados. Siempre usá stop loss y gestioná el riesgo correctamente.<br>
    Nunca operes con dinero que no podés permitirte perder.
  </div>

</div>
</body>
</html>
"""

    subject_emoji = "🚀" if total >= 7 else "📈" if clase == "long" else "🔻" if total <= -7 else "📉" if clase == "short" else "⚠️"
    subject = f"{subject_emoji} ETH {label} | Score {total:+d} | ${price:,.0f}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = MAIL_TO
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, MAIL_TO, msg.as_string())
        log.info(f"✓ Email enviado a {MAIL_TO}")
    except Exception as e:
        log.error(f"Error enviando email: {e}")


# ─────────────────────────────────────────
# COOLDOWN CHECK
# ─────────────────────────────────────────
def should_alert(clase):
    """Evita mandar la misma clase de señal más de una vez por hora."""
    now = time.time()
    if clase == "neutral":
        return False
    cooldown_secs = COOLDOWN_H * 3600
    if last_alert["clase"] == clase and (now - last_alert["ts"]) < cooldown_secs:
        remaining = int((cooldown_secs - (now - last_alert["ts"])) / 60)
        log.info(f"Cooldown activo para '{clase}' — {remaining} min restantes")
        return False
    return True


# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("  ETH Signals Bot arrancando...")
    log.info(f"  Intervalo: {CHECK_EVERY}s  |  Min score: ±{MIN_SCORE}  |  Cooldown: {COOLDOWN_H}h")
    log.info("=" * 50)

    while True:
        try:
            data = analyze()
            clase = data["clase"]

            if should_alert(clase):
                log.info(f"→ Disparando alerta: {data['label']}")
                tags = save_alert(data)
                if MAIL_ENABLED:
                    send_email(data, tags)
                else:
                    log.info("Email desactivado - alerta guardada solo en Supabase")
                last_alert["clase"] = clase
                last_alert["ts"]    = time.time()
            else:
                if clase != "neutral":
                    log.info("→ Sin alerta (cooldown o neutral)")

        except requests.exceptions.RequestException as e:
            log.error(f"Error de red: {e}")
        except Exception as e:
            log.exception(f"Error inesperado: {e}")

        log.info(f"Esperando {CHECK_EVERY}s hasta próxima revisión...\n")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
