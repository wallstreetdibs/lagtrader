import os
import sys
import json
import time
import csv
import zipfile
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
import pandas as pd

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
    "NYSE": (13.5, 20.0),
    "NASDAQ": (13.5, 20.0),
    "TSX": (13.5, 20.0),
    "XETR": (7.0, 15.5),
    "TSE": (0.0, 6.0),
    "KOSPI": (0.0, 6.5),
    "CME": (0.0, 24.0),
    "CRYPTO": (0.0, 24.0)
}

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

def is_market_open(market_name: str) -> bool:
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5 and market_name.upper() not in ["CME", "CRYPTO"]:
        return False
    open_h, close_h = EXCHANGE_HOURS_UTC.get(market_name.upper(), (0.0, 24.0))
    if open_h == 0.0 and close_h == 24.0:
        return True
    utc_hour = now.hour + (now.minute / 60.0)
    return open_h <= utc_hour <= close_h

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
    """Safe trigger time calculation that never crashes on None or invalid formats."""
    if not signal_time_iso or not isinstance(signal_time_iso, str):
        return "N/A"
    try:
        t_sig = datetime.fromisoformat(signal_time_iso)
        if action_time_iso and isinstance(action_time_iso, str):
            t_act = datetime.fromisoformat(action_time_iso)
        else:
            t_act = datetime.now(timezone.utc)
        
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
        """Safe payload generation for the live dashboard."""
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
                        # Handle both new 'action_ticker' and legacy 'ticker'
                        pos_copy["action_ticker"] = pos.get("action_ticker") or pos.get("ticker", "UNKNOWN")
                        pos_copy["action_market"] = pos.get("action_market", "NYSE")
                        pos_copy["direction"] = pos.get("direction", "BUY")
                        pos_copy["qty"] = pos.get("qty", 100)
                        pos_copy["entry_price"] = round(float(pos.get("entry_price", 0.0)), 2)
                        pos_copy["tp_price"] = round(float(pos.get("tp_price", 0.0)), 2)
                        pos_copy["sl_price"] = round(float(pos.get("sl_price", 0.0)), 2)
                        
                        sig_time = pos.get("signal_time") or pos.get("entry_time")
                        act_time = pos.get("action_time") or pos.get("entry_time")
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
            print(f"[ERROR] get_dashboard_payload error: {e}")
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
            print(f"[ERROR] HTTP handler: {e}")
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
        updated = False
        with self.portfolio_mgr.lock:
            for strat_name, strat_info in self.portfolio_mgr.data.get("strategies", {}).items():
                if not isinstance(strat_info, dict):
                    continue
                for pos in strat_info.get("positions", []):
                    if not isinstance(pos, dict):
                        continue
                    if pos.get("status") == "PENDING_MARKET_OPEN":
                        if is_market_open(pos.get("action_market", "NYSE")):
                            pos["status"] = "ACTIVE"
                            pos["action_time"] = datetime.now(timezone.utc).isoformat()
                            updated = True
                            ttt_str = format_trigger_time(pos.get("signal_time"), pos.get("action_time"))
                            print(f"[MARKET OPENED] Activated order for {pos.get('action_ticker')}. Pull Trigger Time: {ttt_str}")

        if updated:
            self.portfolio_mgr.save()

# =====================================================================
# Main Loop
# =====================================================================

if __name__ == "__main__":
    engine = ExecutionEngine()
    print("🚀 LagTrader Engine Running with Live API...")

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

    while True:
        time.sleep(60)
        engine.process_pending_queues()
