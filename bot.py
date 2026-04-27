"""
A3 Multi-Strategy Live Trader
SL: dynamic clip((close-low)/close, 0.1%-5%)  TP: SL 1:1
거래소: Bybit USDT-Perp (demo=DEMO)
"""
import os, sys, time, uuid, logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

import telegram_bot as tg

load_dotenv()

KST = timezone(timedelta(hours=9))
logging.Formatter.converter = lambda *args: datetime.now(KST).timetuple()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s KST [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("A3")

# ── 전략 룰 ──────────────────────────────────────────────────────────
STRATEGY_CONFIGS = {
    "A3_0033": "df['close'] < df['bb_lower']",
    "A3_0039": "df['mfi'] < 20",
    "A3_0096": "(df['pctb'] < 0) & (df['pctb'] > df['pctb'].shift(1))",
    "A3_0012": "(df['close'] > df['sma_200']) & (df['rsi_2'] <= 10)",
    "A3_0070": "(df['slowk'] < 20) & (df['slowk'] > df['slowd'])",
    "A3_0063": "(df['rsi'] < 40) & (df['close'] > df['sma_200'])",
}

# ── 환경변수 ─────────────────────────────────────────────────────────
STRATEGY_ID  = os.environ.get("STRATEGY_ID", "A3_0096")
INTERVAL     = os.environ.get("INTERVAL", "15")
SYMBOL       = os.environ.get("SYMBOL", "BTCUSDT")
RISK_USDT    = float(os.environ.get("RISK_USDT", "10"))
DEMO         = os.environ.get("DEMO", "true").lower() != "false"

if STRATEGY_ID not in STRATEGY_CONFIGS:
    raise ValueError(f"STRATEGY_ID '{STRATEGY_ID}' 지원 안 함. 선택지: {list(STRATEGY_CONFIGS)}")

ENTRY_RULE   = STRATEGY_CONFIGS[STRATEGY_ID]
INTERVAL_MIN = int(INTERVAL)
LABEL        = f"{STRATEGY_ID} {INTERVAL}m"

POLL_SEC         = 30
MAX_BARS         = 48
KLINE_LIMIT      = 250
FILL_WAIT        = INTERVAL_MIN * 3 * 60
FILL_POLL        = 10
MIN_SL           = 0.001
MAX_SL           = 0.05
LAST_SIGNAL_FILE = ".last_signal_ts"


# ── 인디케이터 ────────────────────────────────────────────────────────
def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    bb = ta.bbands(df["close"], length=20, std=2)
    if bb is not None:
        df["bb_lower"] = bb.iloc[:, 0]
        pctb_cols = [c for c in bb.columns if c.startswith("BBP_")]
        if pctb_cols:
            df["pctb"] = bb[pctb_cols[0]]
    mfi = ta.mfi(df["high"], df["low"], df["close"], df["volume"], length=14)
    if mfi is not None:
        df["mfi"] = mfi
    rsi14 = ta.rsi(df["close"], length=14)
    if rsi14 is not None:
        df["rsi"] = rsi14
    rsi2 = ta.rsi(df["close"], length=2)
    if rsi2 is not None:
        df["rsi_2"] = rsi2
    sma200 = ta.sma(df["close"], length=200)
    if sma200 is not None:
        df["sma_200"] = sma200
    stoch = ta.stoch(df["high"], df["low"], df["close"], k=14, d=3, smooth_k=3)
    if stoch is not None and len(stoch.columns) >= 2:
        df["slowk"] = stoch.iloc[:, 0]
        df["slowd"] = stoch.iloc[:, 1]
    return df


def safe_eval(rule: str, df: pd.DataFrame) -> bool:
    try:
        result = eval(rule, {"__builtins__": {}}, {"df": df, "np": np, "pd": pd})
        if hasattr(result, "iloc"):
            return bool(result.iloc[-1])
        return bool(result)
    except Exception as e:
        logger.warning(f"eval 실패: {e}")
        return False


# ── 캔들 ─────────────────────────────────────────────────────────────
def fetch_klines(client: HTTP) -> pd.DataFrame:
    res  = client.get_kline(category="linear", symbol=SYMBOL,
                            interval=INTERVAL, limit=KLINE_LIMIT)
    rows = res["result"]["list"]
    df   = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume","turnover"])
    df   = df.iloc[::-1].reset_index(drop=True)
    for c in ("open","high","low","close","volume"):
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms", utc=True)
    return df


def drop_open_candle(df: pd.DataFrame) -> pd.DataFrame:
    last_ts    = df.iloc[-1]["ts"]
    candle_end = last_ts.to_pydatetime() + timedelta(minutes=INTERVAL_MIN)
    if datetime.now(timezone.utc) < candle_end:
        return df.iloc[:-1].reset_index(drop=True)
    return df


# ── 주문 유틸 ─────────────────────────────────────────────────────────
def fmt_price(price: float, tick: float = 0.10) -> str:
    return f"{round(round(price / tick) * tick, 2):.1f}"


def fmt_qty(qty: float, step: float = 0.001) -> str:
    return f"{round(round(qty / step) * step, 3):.3f}"


# ── 포지션 ───────────────────────────────────────────────────────────
class Position:
    def __init__(self, entry_price: float, qty: float,
                 sl: float, tp: float, sl_pct: float, entry_ts: datetime):
        self.entry_price = entry_price
        self.qty         = qty
        self.sl          = sl
        self.tp          = tp
        self.sl_pct      = sl_pct
        self.entry_ts    = entry_ts


# ── 트레이더 ─────────────────────────────────────────────────────────
class A3Trader:
    def __init__(self):
        self.client = HTTP(
            demo=DEMO,
            api_key=os.environ["BYBIT_API_KEY"],
            api_secret=os.environ["BYBIT_SECRET"],
        )
        self.position: Optional[Position] = None
        self._last_signal_ts              = self._load_last_signal_ts()
        self._wins                        = 0
        self._losses                      = 0
        self._last_daily_date: Optional[str] = None

    def _load_last_signal_ts(self) -> Optional[pd.Timestamp]:
        try:
            with open(LAST_SIGNAL_FILE) as f:
                val = f.read().strip()
                return pd.Timestamp(val) if val else None
        except FileNotFoundError:
            return None

    def _save_last_signal_ts(self, ts: pd.Timestamp) -> None:
        try:
            with open(LAST_SIGNAL_FILE, "w") as f:
                f.write(str(ts))
        except Exception as e:
            logger.warning(f"last_signal_ts 저장 실패: {e}")

    def _stats(self) -> dict:
        total = self._wins + self._losses
        return {
            "wins":    self._wins,
            "losses":  self._losses,
            "winrate": (self._wins / total * 100) if total > 0 else 0.0,
        }

    # ── 진입 ──────────────────────────────────────────────────────────
    def try_enter(self, df: pd.DataFrame) -> None:
        if self.position is not None:
            return

        df = drop_open_candle(df)
        if len(df) < 210:
            return

        last_ts = df.iloc[-1]["ts"]
        if last_ts == self._last_signal_ts:
            return

        df = build_indicators(df)
        if not safe_eval(ENTRY_RULE, df):
            return

        try:
            pos_res = self.client.get_positions(category="linear", symbol=SYMBOL)
            for p in pos_res["result"]["list"]:
                if float(p.get("size", 0)) > 0:
                    logger.info("Bybit 포지션 있음 — 스킵")
                    return
            ord_res = self.client.get_open_orders(category="linear", symbol=SYMBOL)
            if ord_res["result"]["list"]:
                logger.info("미체결 주문 있음 — 스킵")
                return
        except Exception as e:
            logger.warning(f"상태 조회 실패: {e}")
            return

        entry  = df.iloc[-1]["close"]
        low    = df.iloc[-1]["low"]
        sl_pct = float(np.clip((entry - low) / entry, MIN_SL, MAX_SL))
        sl     = entry * (1 - sl_pct)
        tp     = entry * (1 + sl_pct)
        qty    = RISK_USDT / (entry * sl_pct)

        logger.info(f"신호 감지 | entry={entry:.2f} sl={sl:.2f} tp={tp:.2f} sl%={sl_pct*100:.2f} qty={qty:.4f}")
        self._last_signal_ts = last_ts
        self._save_last_signal_ts(last_ts)

        try:
            link_id  = uuid.uuid4().hex[:16]
            res      = self.client.place_order(
                category="linear", symbol=SYMBOL, side="Buy",
                orderType="Limit", qty=fmt_qty(qty), price=fmt_price(entry),
                timeInForce="GTC",
                stopLoss=fmt_price(sl), takeProfit=fmt_price(tp),
                orderLinkId=link_id,
            )
            order_id = res["result"]["orderId"]
            logger.info(f"주문 제출: {order_id}")
        except Exception as e:
            logger.error(f"주문 실패: {e}")
            tg.send_error(f"주문 실패: {e}")
            return

        self._wait_fill(order_id, entry, sl, tp, qty, sl_pct)

    def _wait_fill(self, order_id: str, entry: float, sl: float,
                   tp: float, qty: float, sl_pct: float) -> None:
        deadline = time.time() + FILL_WAIT
        while time.time() < deadline:
            time.sleep(FILL_POLL)
            try:
                res    = self.client.get_order_history(
                    category="linear", symbol=SYMBOL, orderId=order_id, limit=1)
                orders = res["result"]["list"]
            except Exception as e:
                logger.warning(f"체결 조회 실패: {e}")
                continue

            if not orders:
                continue
            o      = orders[0]
            status = o.get("orderStatus", "")

            if status == "Filled":
                actual_entry = float(o.get("avgPrice", entry))
                actual_qty   = float(o.get("qty", qty))
                self.position = Position(
                    entry_price=actual_entry, qty=actual_qty,
                    sl=sl, tp=tp, sl_pct=sl_pct,
                    entry_ts=datetime.now(timezone.utc),
                )
                logger.info(f"체결: {actual_entry:.2f} qty={actual_qty}")
                tg.send_entry(
                    strategy=LABEL,
                    entry_price=actual_entry, sl=sl, tp=tp,
                    qty=actual_qty, sl_pct=sl_pct * 100,
                    stats=self._stats(),
                )
                self._verify_sltp()
                return

            if status in ("Cancelled", "Rejected", "Expired"):
                logger.info(f"주문 {status} — 포기")
                return

        logger.info("체결 타임아웃 — 주문 취소")
        try:
            self.client.cancel_order(category="linear", symbol=SYMBOL, orderId=order_id)
        except Exception as e:
            logger.warning(f"취소 실패: {e}")

    def _verify_sltp(self) -> None:
        if self.position is None:
            return
        try:
            res   = self.client.get_positions(category="linear", symbol=SYMBOL)
            plist = res["result"]["list"]
            for p in plist:
                if float(p.get("size", 0)) > 0:
                    has_sl = float(p.get("stopLoss", 0)) > 0
                    has_tp = float(p.get("takeProfit", 0)) > 0
                    if has_sl and has_tp:
                        logger.info("SL/TP 확인 완료")
                        return
                    logger.warning("SL/TP 미첨부 — 별도 설정")
                    self.client.set_trading_stop(
                        category="linear", symbol=SYMBOL,
                        stopLoss=fmt_price(self.position.sl),
                        takeProfit=fmt_price(self.position.tp),
                        positionIdx=0,
                    )
                    logger.info("SL/TP 재설정 완료")
                    return
        except Exception as e:
            logger.error(f"SL/TP 검증 실패: {e}")
            tg.send_error(f"SL/TP 검증 실패: {e}")

    # ── 포지션 모니터링 ───────────────────────────────────────────────
    def check_position(self) -> None:
        if self.position is None:
            return
        pos = self.position

        try:
            res  = self.client.get_positions(category="linear", symbol=SYMBOL)
            size = sum(float(p.get("size", 0)) for p in res["result"]["list"])
        except Exception as e:
            logger.warning(f"포지션 체크 실패: {e}")
            return

        if size == 0:
            self._record_close(pos)
            return

        elapsed_min = (datetime.now(timezone.utc) - pos.entry_ts).total_seconds() / 60
        if elapsed_min >= MAX_BARS * INTERVAL_MIN:
            logger.info(f"최대 보유 초과 ({elapsed_min:.0f}분) — 강제 청산")
            self._force_close(pos)

    def _record_close(self, pos: Position) -> None:
        time.sleep(2)
        try:
            res     = self.client.get_closed_pnl(category="linear", symbol=SYMBOL, limit=1)
            records = res["result"]["list"]
        except Exception as e:
            logger.warning(f"PnL 조회 실패: {e}")
            self.position = None
            return

        if not records:
            self.position = None
            return

        rec        = records[0]
        pnl        = float(rec.get("closedPnl", 0))
        exit_price = float(rec.get("avgExitPrice", pos.entry_price))
        r_unit     = pnl / RISK_USDT

        if pnl > 0:
            self._wins += 1
            status = "WIN"
        else:
            self._losses += 1
            status = "LOSS"

        logger.info(f"청산: {status}  exit={exit_price:.2f}  R={r_unit:+.2f}")
        tg.send_exit(
            strategy=LABEL,
            status=status, entry_price=pos.entry_price,
            exit_price=exit_price, r_unit=r_unit, stats=self._stats(),
        )
        self.position = None

    def _force_close(self, pos: Position) -> None:
        try:
            self.client.cancel_all_orders(category="linear", symbol=SYMBOL)
            self.client.place_order(
                category="linear", symbol=SYMBOL, side="Sell",
                orderType="Market", qty=fmt_qty(pos.qty),
                reduceOnly=True,
            )
        except Exception as e:
            logger.error(f"강제 청산 실패: {e}")
            tg.send_error(f"강제 청산 실패: {e}")
            return

        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(3)
            try:
                res  = self.client.get_positions(category="linear", symbol=SYMBOL)
                size = sum(float(p.get("size", 0)) for p in res["result"]["list"])
                if size == 0:
                    break
            except Exception:
                pass
        self._record_close(pos)

    # ── 일일 보고서 ───────────────────────────────────────────────────
    def maybe_daily_report(self) -> None:
        now_kst  = datetime.now(KST)
        date_str = now_kst.strftime("%Y-%m-%d")
        if now_kst.hour == 9 and self._last_daily_date != date_str:
            total = self._wins + self._losses
            tg.send_daily_report(
                strategy=LABEL,
                wins=self._wins, losses=self._losses,
                win_rate=(self._wins / total * 100) if total > 0 else 0.0,
            )
            self._last_daily_date = date_str

    # ── 메인 루프 ─────────────────────────────────────────────────────
    def run(self) -> None:
        logger.info(f"시작: {LABEL} | {'DEMO' if DEMO else 'LIVE'} | RISK=${RISK_USDT}")
        tg.send_startup(strategy=LABEL, demo=DEMO, risk_usdt=RISK_USDT)

        while True:
            try:
                df = fetch_klines(self.client)
                self.check_position()
                self.try_enter(df)
                self.maybe_daily_report()
            except KeyboardInterrupt:
                tg.send_shutdown(strategy=LABEL)
                break
            except Exception as e:
                logger.error(f"루프 에러: {e}", exc_info=True)
                tg.send_error(str(e)[:300])

            time.sleep(POLL_SEC)


if __name__ == "__main__":
    A3Trader().run()
