import os
import sys
import json
import time
import csv
import zipfile
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, date
import pandas as pd

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

# =====================================================================
# Configuration & Constants
# =====================================================================

DATA_DIR = "data"
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
PORTFOLIO_PATH = os.path.join(DATA_DIR, "portfolio_state.json")
HISTORY_PATH = os.path.join(DATA_DIR, "trade_history.csv")

HTTP_PORT = int(os.environ.get("PORT", 8080))

STRATEGY_ROSTER = [
    "ASX_ADR_Arbitrage", "US_Earnings_Lag", "Inventory_Drift_Reversal",
    "Futures_Lead_Front_Run", "Crypto_FinTech_Echo", "Sentiment_Echo",
    "Immediate_Index_Proxy", "Nikkei_ADR_Front_Run", "WTI_Crude_Lag",
    "Gold_Futures_Echo", "Biotech_News_Lag", "Cross_Listed_Pair_Fade",
    "Time_Zone_Momentum_Relay", "FX_Adjusted_Earnings_Arb", "Commodity_Proxy_Lag",
    "ETF_NAV_Window_Arb", "Nikkei_Tech_Relay", "London_Metals_Catchup",
    "Treasury_Shockwave", "Canadian_Energy_Echo", "ETF_Creation_Lag",
    "SKHY_ADR_FX_Neutralization", "SKHY_HBM_Supply_Chain", "SKHY_Post_Market_KOSPI"
]

EXCHANGE_HOURS_UTC = {
    "NYSE": (13.5, 20.0),    # 9:30 AM - 4:00 PM US ET
    "NASDAQ": (13.5, 20.0),
    "TSX": (13.5, 20.0),     # Toronto
    "XETR": (7.0, 15.5),     # Frankfurt
    "LSE": (7.0, 15.5),      # London
    "OMX": (7.0, 15.0),      # Nordic / Copenhagen
    "TSE": (0.0, 6.5),       # Tokyo (9:00 AM - 3:30 PM JST)
    "KOSPI": (0.0, 6.5),     # Seoul (9:00 AM - 3:30 PM KST)
    "ASX": (23.0, 6.0),      # Sydney (opens 23:00 UTC previous day to 06:00 UTC)
    "CME": (0.0, 24.0),      # Futures
    "CRYPTO": (0.0, 24.0)    # 24/7
}

# =====================================================================
# Market Calendar & Holiday Intelligence
# =====================================================================

def is_us_holiday(d: date) -> bool:
    """Calculates major US market holidays where NYSE/NASDAQ are closed."""
    # New Year's Day (Jan 1)
    if d.month == 1 and d.day == 1:
        return True
    # MLK Day: 3rd Monday in January
    if d.month == 1 and d.weekday() == 0 and 15 <= d.day <= 21:
        return True
    # Presidents' Day: 3rd Monday in February
    if d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21:
        return True
    # Memorial Day: Last Monday in May
    if d.month == 5 and d.weekday() == 0 and d.day >= 25:
        return True
    # Juneteenth: June 19
    if d.month == 6 and d.day == 19:
        return True
    # Independence Day: July 4
    if d.month == 7 and d.day == 4:
        return True
    # Labor Day: 1st Monday in September (e.g. today, Sept 7, 2026!)
    if d.month == 9 and d.weekday() == 0 and 1 <= d.day <= 7:
        return True
    # Thanksgiving: 4th Thursday in November
    if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
        return True
    # Christmas Day: Dec 25
    if d.month == 12 and d.day == 25:
        return True
    return False

def is_market_open(market_name: str, now: datetime = None) -> bool:
    """Smart market open check that handles timezones, Sunday Asian open, and holidays."""
    if now is None:
        now = datetime.now(timezone.utc)
    market = market_name.upper()

    if market == "CRYPTO":
        return True

    weekday = now.weekday()  # 0 = Monday, ..., 5 = Saturday, 6 = Sunday
    utc_hour = now.hour + (now.minute / 60.0)

    # Saturday: All equity & futures markets are closed worldwide
    if weekday == 5:
        return False

    # Sunday:
    if weekday == 6:
        # CME futures open Sunday at 22:00 UTC (6:00 PM ET)
        if market == "CME":
            return utc_hour >= 22.0
        # ASX (Australia) opens Sunday at 23:00 UTC (Monday 9:00 AM Sydney)
        if market == "ASX":
            return utc_hour >= 23.0
        # All other stock markets are closed on Sunday
        return False

    # Friday closing: CME futures pause Friday at 21:00 UTC
    if weekday == 4 and market == "CME" and utc_hour >= 21.0:
        return False

    # US market holiday check
    if market in ["NYSE", "NASDAQ"] and is_us_holiday(now.date()):
        return False

    open_h, close_h = EXCHANGE_HOURS_UTC.get(market, (0.0, 24.0))
    if open_h == 0.0 and close_h == 24.0:
        return True

    if open_h > close_h:
        # Crosses midnight UTC (like ASX: 23:00 to 06:00 UTC)
        return utc_hour >= open_h or utc_hour <= close_h

    return open_h <= utc_hour <= close_h

# =====================================================================
# Utilities
# =====================================================================

def ensure_environment():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "strategy", "signal_ticker", "action_ticker",
                "action", "qty", "price", "gross_pnl", "fee", "net_pnl",
                "trigger_time", "reason"
            ])

def calculate_dynamic_tp_sl(entry_price: float, signal_discrepancy_pct: float, beta: float = 1.0, atr_14: float = 0.50, direction: str = "BUY"):
    expected_move_pct = (signal_discrepancy_pct * beta) * 0.80
    if direction.upper() == "BUY":
        tp_price = round(entry_price * (1 + (expected_move_pct / 100.0)), 2)
        sl_price = round(entry_price - (1.5 * atr_14), 2)
    else:
        tp_price = round(entry_price * (1 - (expected_move_pct / 100.0)), 2)
        sl_price = round(entry_price + (1.5 * atr_14), 2)
    return tp_price, sl_price

def format_trigger_time(signal_time_iso, action_time_iso=None) -> str:
    """Calculates Pull the Trigger Time including all queuing time."""
    if not signal_time_iso or not isinstance(signal_time_iso, str):
        return "N/A"
    try:
        t_sig = datetime.fromisoformat(signal_time_iso)
        t_act = datetime.fromisoformat(action_time_iso) if action_time_iso else datetime.now(timezone.utc)
        elapsed_sec = max(0, int((t_act - t_sig).total_seconds()))
        hours = elapsed_sec // 3600
        minutes = (elapsed_sec % 3600) // 60
        seconds = elapsed_sec % 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"
    except Exception:
        return "N/A"

def fetch_live_price(ticker: str):
    """Ultra-lightweight price fetcher using yfinance fast_info (minimal RAM/CPU)."""
    if not YFINANCE_AVAILABLE:
        return None
    try:
        t = yf.Ticker(ticker)
        # fast_info only retrieves the latest quote without downloading historical tables
        price = t.fast_info.get("last_price")
        if price is not None and not pd.isna(price) and price > 0:
            return round(float(price), 2)
        # Fallback to single 1-minute candle
        hist = t.history(period="1d", interval="1m")
        if not hist.empty and "Close" in hist:
            last_val = hist["Close"].iloc[-1]
            if not pd.isna(last_val) and last_val > 0:
                return round(float(last_val), 2)
    except Exception:
        pass
    return None

# =====================================================================
# Portfolio Manager
# =====================================================================

class PortfolioManager:
    def __init__(self, filepath=PORTFOLIO_PATH):
        self.filepath = filepath
        self.lock = threading.Lock()
        self.data = self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                    self._reconcile_roster(data)
                    return data
            except Exception as e:
                print(f"[WARN] Error reading portfolio JSON: {e}. Resetting.")
        return self._build_default_state()

    def _build_default_state(self):
        state = {"total_capital": 240000.0, "strategies": {}}
        for strat in STRATEGY_ROSTER:
            state["strategies"][strat] = {"allocated": 10000.0, "cash": 10000.0, "positions": []}
        return state

    def _reconcile_roster(self, data):
        if "strategies" not in data or not isinstance(data["strategies"], dict):
            data["strategies"] = {}
        for strat in STRATEGY_ROSTER:
            if strat not in data["strategies"] or not isinstance(data["strategies"][strat], dict):
                data["strategies"][strat] = {"allocated": 10000.0, "cash": 10000.0, "positions": []}

    def save(self):
        with self.lock:
            with open(self.filepath, "w") as f:
                json.dump(self.data, f, indent=2)

    def get_strategy_stats(self):
        stats = {strat: {"wins": 0, "losses": 0, "win_rate": 0.0} for strat in STRATEGY_ROSTER}
        if os.path.exists(HISTORY_PATH):
            try:
                df = pd.read_csv(HISTORY_PATH)
                if not df.empty and "strategy" in df.columns and "net_pnl" in df.columns:
                    for strat, group in df.groupby("strategy"):
                        wins = int((group["net_pnl"] > 0).sum())
                        losses = int((group["net_pnl"] <= 0).sum())
                        total = wins + losses
                        win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0
                        stats[strat] = {"wins": wins, "losses": losses, "win_rate": win_rate}
            except Exception as e:
                print(f"[WARN] CSV parse failure: {e}")
        return stats

    def get_dashboard_payload(self):
        try:
            stats = self.get_strategy_stats()
            total_wins = sum(s["wins"] for s in stats.values())
            total_losses = sum(s["losses"] for s in stats.values())
            total_trades = total_wins + total_losses
            overall_win_rate = round((total_wins / total_trades) * 100, 1) if total_trades > 0 else 0.0

            all_orders = []
            with self.lock:
                strategies_data = self.data.get("strategies", {})
                for strat_name, strat in strategies_data.items():
                    if not isinstance(strat, dict):
                        continue
                    for pos in strat.get("positions", []):
                        if not isinstance(pos, dict):
                            continue
                        pos_copy = dict(pos)
                        pos_copy["strategy"] = strat_name
                        pos_copy["status"] = pos.get("status", "ACTIVE")
                        pos_copy["action_ticker"] = pos.get("action_ticker") or pos.get("ticker", "UNKNOWN")
                        pos_copy["action_market"] = pos.get("action_market", "NYSE")
                        pos_copy["direction"] = pos.get("direction", "BUY")
                        pos_copy["qty"] = pos.get("qty", 100)
                        pos_copy["entry_price"] = round(float(pos.get("entry_price", 0.0)), 2)
                        pos_copy["tp_price"] = round(float(pos.get("tp_price", 0.0)), 2)
                        pos_copy["sl_price"] = round(float(pos.get("sl_price", 0.0)), 2)
                        
                        sig_time = pos.get("signal_time") or pos.get("entry_time")
                        act_time = pos.get("action_time")
                        pos_copy["pull_trigger_time"] = format_trigger_time(sig_time, act_time)
                        all_orders.append(pos_copy)

            strat_list = []
            for s in STRATEGY_ROSTER:
                s_dict = self.data.get("strategies", {}).get(s, {})
                allocated = s_dict.get("allocated", 10000.0) if isinstance(s_dict, dict) else 10000.0
                cash = s_dict.get("cash", 10000.0) if isinstance(s_dict, dict) else 10000.0
                s_stat = stats.get(s, {"wins": 0, "losses": 0, "win_rate": 0.0})
                strat_list.append({
                    "name": s,
                    "wins": s_stat["wins"],
                    "losses": s_stat["losses"],
                    "win_rate": s_stat["win_rate"],
                    "allocated": round(float(allocated), 2),
                    "cash": round(float(cash), 2)
                })

            return {
                "status": "online",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kpi": {
                    "total_capital": round(float(self.data.get("total_capital", 240000.0)), 2),
                    "total_wins": total_wins,
                    "total_losses": total_losses,
                    "win_rate": overall_win_rate
                },
                "orders": all_orders,
                "strategies": strat_list
            }
        except Exception as e:
            return {
                "status": "online",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kpi": {"total_capital": 240000.0, "total_wins": 0, "total_losses": 0, "win_rate": 0.0},
                "orders": [],
                "strategies": [{"name": s, "wins": 0, "losses": 0, "win_rate": 0.0, "allocated": 10000.0, "cash": 10000.0} for s in STRATEGY_ROSTER]
            }

# =====================================================================
# Web API & Health-Check Server
# =====================================================================

ENGINE_INSTANCE = None

class DashboardAPIHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        try:
            parsed_path = self.path.split("?")[0]
            if parsed_path == "/api/data":
                payload = ENGINE_INSTANCE.portfolio_mgr.get_dashboard_payload() if ENGINE_INSTANCE else {"status": "starting"}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(body)

            elif parsed_path == "/api/backup":
                if os.path.exists(HISTORY_PATH):
                    with open(HISTORY_PATH, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-type", "text/csv")
                    self.send_header("Content-Disposition", "attachment; filename=trade_history.csv")
                    self.send_header("Content-Length", str(len(data)))
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    body = b"No trade history yet."
                    self.send_response(404)
                    self.send_header("Content-type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(body)

            else:
                response = {"status": "online", "system": "LagTrader Engine", "timestamp": datetime.now(timezone.utc).isoformat()}
                body = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(body)

        except Exception as e:
            try:
                err_body = json.dumps({"error": str(e)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(err_body)
            except Exception:
                pass

    def log_message(self, format, *args):
        return

def start_server(port=HTTP_PORT):
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[SERVER] Listening on port {port} (/api/data & /)")

# =====================================================================
# Execution Engine
# =====================================================================

class ExecutionEngine:
    def __init__(self):
        global ENGINE_INSTANCE
        ensure_environment()
        self.portfolio_mgr = PortfolioManager()
        ENGINE_INSTANCE = self
        start_server()

    def process_signal(self, signal_payload: dict):
        signal_time = signal_payload.get("signal_time", datetime.now(timezone.utc).isoformat())
        strat_name = signal_payload["strategy"]
        action_market = signal_payload.get("action_market", "NYSE")
        
        market_open = is_market_open(action_market)
        order_status = "ACTIVE" if market_open else "PENDING_MARKET_OPEN"
        action_time = datetime.now(timezone.utc).isoformat() if market_open else None

        entry_price = signal_payload.get("entry_price", 100.0)
        discrepancy = signal_payload.get("discrepancy_pct", 1.5)
        beta = signal_payload.get("beta", 1.0)
        atr_14 = signal_payload.get("atr_14", 0.50)
        direction = signal_payload.get("direction", "BUY")

        tp, sl = calculate_dynamic_tp_sl(entry_price, discrepancy, beta, atr_14, direction)

        position_record = {
            "order_id": f"ORD_{int(time.time()*1000)}",
            "status": order_status,
            "strategy": strat_name,
            "signal_ticker": signal_payload.get("signal_ticker"),
            "signal_market": signal_payload.get("signal_market"),
            "action_ticker": signal_payload.get("action_ticker"),
            "action_market": action_market,
            "direction": direction,
            "qty": signal_payload.get("qty", 100),
            "entry_price": entry_price,
            "tp_price": tp,
            "sl_price": sl,
            "signal_time": signal_time,
            "action_time": action_time
        }

        strat_dict = self.portfolio_mgr.data["strategies"].setdefault(
            strat_name, {"allocated": 10000.0, "cash": 10000.0, "positions": []}
        )
        strat_dict["positions"].append(position_record)
        self.portfolio_mgr.save()

        ttt_str = format_trigger_time(signal_time, action_time)
        print(f"[{order_status}] Strategy: {strat_name} | Action: {position_record['action_ticker']} | TP: {tp} | SL: {sl} | Trigger Time: {ttt_str}")

    def process_pending_queues(self):
        """Activates queued orders when the Action Market opens, updating entry price to real market open."""
        updated = False
        with self.portfolio_mgr.lock:
            for strat_name, strat_info in self.portfolio_mgr.data.get("strategies", {}).items():
                if not isinstance(strat_info, dict):
                    continue
                for pos in strat_info.get("positions", []):
                    if not isinstance(pos, dict):
                        continue
                    if pos.get("status") == "PENDING_MARKET_OPEN":
                        action_mkt = pos.get("action_market", "NYSE")
                        if is_market_open(action_mkt):
                            ticker = pos.get("action_ticker")
                            real_open_price = fetch_live_price(ticker)
                            
                            # If we got a real open price, update entry price & TP/SL to remove gap bias
                            if real_open_price and real_open_price > 0:
                                pos["entry_price"] = real_open_price
                                direction = pos.get("direction", "BUY")
                                tp, sl = calculate_dynamic_tp_sl(real_open_price, 1.5, 1.0, 0.50, direction)
                                pos["tp_price"] = tp
                                pos["sl_price"] = sl

                            pos["status"] = "ACTIVE"
                            pos["action_time"] = datetime.now(timezone.utc).isoformat()
                            updated = True
                            ttt_str = format_trigger_time(pos.get("signal_time"), pos.get("action_time"))
                            print(f"[MARKET OPENED] Order placed for {ticker} at ${pos['entry_price']}. Pull Trigger Time: {ttt_str}")

        if updated:
            self.portfolio_mgr.save()

    def check_active_positions_tp_sl(self):
        """Monitors open positions and exits when TP or SL is reached (only when the exchange is open!)."""
        positions_to_close = []

        with self.portfolio_mgr.lock:
            for strat_name, strat_info in self.portfolio_mgr.data.get("strategies", {}).items():
                if not isinstance(strat_info, dict):
                    continue
                for pos in strat_info.get("positions", []):
                    if not isinstance(pos, dict):
                        continue
                    if pos.get("status") == "ACTIVE":
                        action_mkt = pos.get("action_market", "NYSE")
                        
                        # ZERO network queries if that stock's exchange is closed!
                        if not is_market_open(action_mkt):
                            continue

                        ticker = pos.get("action_ticker") or pos.get("ticker")
                        if not ticker:
                            continue

                        current_price = fetch_live_price(ticker)
                        if not current_price:
                            continue

                        direction = pos.get("direction", "BUY")
                        tp = pos.get("tp_price", 999999)
                        sl = pos.get("sl_price", 0)

                        if direction in ["BUY", "LONG"]:
                            if current_price >= tp:
                                positions_to_close.append((strat_name, ticker, current_price, "TAKE_PROFIT"))
                            elif current_price <= sl:
                                positions_to_close.append((strat_name, ticker, current_price, "STOP_LOSS"))
                        else:  # SELL / SHORT
                            if current_price <= tp:
                                positions_to_close.append((strat_name, ticker, current_price, "TAKE_PROFIT"))
                            elif current_price >= sl:
                                positions_to_close.append((strat_name, ticker, current_price, "STOP_LOSS"))

        # Close triggered positions
        for strat_name, ticker, exit_price, reason in positions_to_close:
            self.close_position(strat_name, ticker, exit_price, reason)

    def close_position(self, strat_name: str, action_ticker: str, exit_price: float, reason: str = "TAKE_PROFIT"):
        strat_info = self.portfolio_mgr.data["strategies"].get(strat_name)
        if not strat_info:
            return

        remaining = []
        with self.portfolio_mgr.lock:
            for pos in strat_info.get("positions", []):
                ticker = pos.get("action_ticker") or pos.get("ticker")
                if ticker == action_ticker and pos.get("status") == "ACTIVE":
                    qty = pos.get("qty", 100)
                    entry_price = pos.get("entry_price", exit_price)
                    direction = pos.get("direction", "BUY")

                    multiplier = 1 if direction in ["BUY", "LONG"] else -1
                    gross_pnl = round((exit_price - entry_price) * qty * multiplier, 2)
                    fee = round(max(1.00, qty * 0.005), 2)
                    net_pnl = round(gross_pnl - fee, 2)
                    
                    sig_time = pos.get("signal_time") or pos.get("entry_time")
                    act_time = pos.get("action_time")
                    ttt_str = format_trigger_time(sig_time, act_time)

                    with open(HISTORY_PATH, "a", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow([
                            datetime.now(timezone.utc).isoformat(),
                            strat_name,
                            pos.get("signal_ticker", "-"),
                            ticker,
                            f"CLOSE_{direction}",
                            qty,
                            exit_price,
                            gross_pnl,
                            fee,
                            net_pnl,
                            ttt_str,
                            reason
                        ])

                    strat_info["cash"] = round(strat_info.get("cash", 10000.0) + net_pnl, 2)
                    print(f"🎯 [TRADE CLOSED] {strat_name} ({ticker}) at ${exit_price} | PnL: ${net_pnl} | Reason: {reason} | Trigger Time: {ttt_str}")
                else:
                    remaining.append(pos)

            strat_info["positions"] = remaining
        self.portfolio_mgr.save()

# =====================================================================
# Main Loop (Resource-Efficient Polling)
# =====================================================================

if __name__ == "__main__":
    engine = ExecutionEngine()
    print("🚀 LagTrader Engine Running. Smart 24/7 Market Monitor Active.")

    test_signal = {
        "strategy": "SKHY_ADR_FX_Neutralization",
        "signal_ticker": "000660.KS",
        "signal_market": "KOSPI",
        "action_ticker": "HXSCL",
        "action_market": "NYSE",
        "direction": "BUY",
        "qty": 200,
        "entry_price": 18.45,
        "discrepancy_pct": 2.1,
        "beta": 1.15,
        "atr_14": 0.35,
        "signal_time": datetime.now(timezone.utc).isoformat()
    }

    engine.process_signal(test_signal)
    engine.process_pending_queues()

    # Smart background loop:
    # Checks pending queues and TP/SL every 60 seconds
    while True:
        try:
            engine.process_pending_queues()
            engine.check_active_positions_tp_sl()
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
        time.sleep(60)
