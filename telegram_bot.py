import os, logging
from datetime import datetime, timezone, timedelta
import requests

logger = logging.getLogger("TG")
KST = timezone(timedelta(hours=9))


def _send(text: str) -> bool:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("텔레그램 미설정")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        logger.error(f"텔레그램 예외: {e}")
        return False


def send_startup(strategy: str, demo: bool, risk_usdt: float) -> None:
    mode = "DEMO" if demo else "LIVE"
    _send(
        f"🚀 <b>시작: {strategy}</b>\n"
        f"\n"
        f"모드: <b>{mode}</b>\n"
        f"심볼: BTCUSDT\n"
        f"SL/TP: 동적 1:1 (진입봉 저점 기준)\n"
        f"리스크: ${risk_usdt:.0f}/트레이드\n"
        f"시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}"
    )


def send_entry(strategy: str, entry_price: float, sl: float, tp: float,
               qty: float, sl_pct: float, stats: dict) -> None:
    wins   = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total  = wins + losses
    wr_str = f"{stats.get('winrate', 0):.1f}%" if total > 0 else "—"
    _send(
        f"🟢 <b>진입: {strategy} BTCUSDT 롱</b>\n"
        f"\n"
        f"진입가: <code>${entry_price:,.2f}</code>\n"
        f"손절가: <code>${sl:,.2f}</code>  (-{sl_pct:.2f}%)\n"
        f"목표가: <code>${tp:,.2f}</code>  (+{sl_pct:.2f}%)\n"
        f"수량: {qty:.4f} BTC\n"
        f"\n"
        f"📊 누적: {wins}승 {losses}패  WR {wr_str}\n"
        f"시각: {datetime.now(KST).strftime('%m/%d %H:%M KST')}"
    )


def send_exit(strategy: str, status: str, entry_price: float, exit_price: float,
              r_unit: float, stats: dict, timeout: bool = False) -> None:
    wins   = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total  = wins + losses
    wr_str = f"{stats.get('winrate', 0):.1f}%" if total > 0 else "—"

    if status == "WIN":
        emoji, title = "🏆", "익절 (WIN)"
    elif status == "LOSS":
        emoji, title = "💥", "손절 (LOSS)"
    else:
        emoji, title = "⏹️", f"종결 ({status})"

    timeout_str = "  [시간초과 청산]" if timeout else ""
    _send(
        f"{emoji} <b>{title}: {strategy}{timeout_str}</b>\n"
        f"\n"
        f"진입가: <code>${entry_price:,.2f}</code>\n"
        f"청산가: <code>${exit_price:,.2f}</code>\n"
        f"결과: <b>{r_unit:+.2f}R</b>\n"
        f"\n"
        f"📊 누적: {wins}승 {losses}패  WR {wr_str}\n"
        f"시각: {datetime.now(KST).strftime('%m/%d %H:%M KST')}"
    )


def send_daily_report(strategy: str, wins: int, losses: int, win_rate: float) -> None:
    total    = wins + losses
    wr_str   = f"{win_rate:.1f}%" if total > 0 else "—"
    date_kst = datetime.now(KST).strftime("%Y-%m-%d")
    _send(
        f"☀️ <b>일일 보고서 ({date_kst})</b>\n"
        f"\n"
        f"전략: {strategy} BTCUSDT\n"
        f"누적 매매: {total}건  ({wins}승 {losses}패)\n"
        f"승률: <b>{wr_str}</b>\n"
        f"\n"
        f"시각: {datetime.now(KST).strftime('%H:%M KST')}"
    )


def send_shutdown(strategy: str, reason: str = "정상 종료") -> None:
    _send(
        f"🛑 <b>종료: {strategy}</b>\n"
        f"사유: {reason}\n"
        f"시각: {datetime.now(KST).strftime('%m/%d %H:%M KST')}"
    )


def send_error(msg: str) -> None:
    _send(
        f"⚠️ <b>시스템 에러</b>\n"
        f"<code>{msg[:400]}</code>\n"
        f"시각: {datetime.now(KST).strftime('%m/%d %H:%M KST')}"
    )
