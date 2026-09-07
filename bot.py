import os
import sys
import json
import time
import csv
import zipfile
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
import pandas as pd

try:
    import gspread
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

# =====================================================================
# Configuration & Constants
# =====================================================================

DATA_DIR = "data"
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
PORTFOLIO_PATH = os.path.join(DATA_DIR, "portfolio_state.json")
HISTORY_PATH = os.path.join(DATA_DIR, "trade_history.csv")
GOOGLE_CREDENTIALS_PATH = "google_credentials.json"
GOOGLE_SHEET_NAME = "LagTrader_Dashboard_Data"
HTTP_PORT = 8080

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
# Utilities & Dynamic Statistical Functions
# =====================================================================

def ensure_environment():
    """Ensure data storage directory, backup directory, and trade history CSV exist."""
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

def create_offline_backup():
    """Generates timestamped zip archives of portfolio state and trade logs."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_zip = os.path.join(BACKUP_DIR, f"lagtrader_backup_{timestamp}.zip")
    
    with zipfile.ZipFile(backup_zip, 'w') as zipf:
        if os.path.exists(PORTFOLIO_PATH):
            zipf.write(PORTFOLIO_PATH, arcname="portfolio_state.json")
        if os.path.exists(HISTORY_PATH):
            zipf.write(HISTORY_PATH, arcname="trade_history.csv")
            
    print(f"[BACKUP] Offline backup created: {backup_zip}")

def is_market_open(market_name: str) -> bool:
    """Checks whether the specified target market is currently open in UTC."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5 and market_name.upper() not in ["CME", "CRYPTO"]:
        return False
    
    open_h, close_h = EXCHANGE_HOURS_UTC.get(market_name.upper(), (0.0, 24.0))
    if open_h == 0.0 and close_h == 24.0:
        return True
    
    utc_hour = now.hour + (now.minute / 60.0)
    return open_h <= utc_hour <= close_h

def calculate_dynamic_tp_sl(entry_price: float, signal_discrepancy_pct: float, beta: float = 1.0, atr_14: float = 0.50, direction: str = "BUY"):
    """
    Dynamic TP/SL engine:
    TP = Entry +/- (Signal Discrepancy % * Beta * 0.80) to capture 80% of mean-reversion move.
    SL = Entry -/+ (1.5 * ATR_14) to shield against noise.
    """
    expected_move_pct = (signal_discrepancy_pct * beta) * 0.80
    
    if direction.upper() == "BUY":
        tp_price = round(entry_price * (1 + (expected_move_pct / 100.0)), 2)
        sl_price = round(entry_price - (1.5 * atr_14), 2)
    else:
        tp_price = round(entry_price * (1 - (expected_move_pct / 100.0)), 2)
        sl_price = round(entry_price + (1.5 * atr_14), 2)
        
    return tp_price, sl_price

def format_trigger_time(signal_time_iso: str, action_time_iso: str = None) -> str:
    """Calculates Pull the Trigger Time in human-readable hours/minutes/seconds."""
    t_sig = datetime.fromisoformat(signal_time_iso)
    t_act = datetime.fromisoformat(action_time_iso) if action_time_iso else datetime.now(timezone.utc)
    
    elapsed_sec = int((t_act - t_sig).total_seconds())
    hours = elapsed_sec // 3600
    minutes = (elapsed_sec % 3600) // 60
    seconds = elapsed_sec % 60
    
    if hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

# =====================================================================
# Embedded Health-Check Server (For Keep-Alive Pings)
# =====================================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        response = {"status": "online", "system": "LagTrader Engine", "timestamp": datetime.now(timezone.utc).isoformat()}
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format, *args):
        return  # Suppress HTTP server stdout logs

def start_health_check_server(port=HTTP_PORT):
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[SERVER] Health check endpoint listening on port {port} (/ping)")

# =====================================================================
# Portfolio Manager
# =====================================================================

class PortfolioManager:
    def __init__(self, filepath=PORTFOLIO_PATH):
        self.filepath = filepath
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
            state["strategies"][strat] = {
                "allocated": 10000.0,
                "cash": 10000.0,
                "positions": []
            }
        return state

    def _reconcile_roster(self, data):
        if "strategies" not in data:
            data["strategies"] = {}
        for strat in STRATEGY_ROSTER:
            if strat not in data["strategies"]:
                data["strategies"][strat] = {
                    "allocated": 10000.0,
                    "cash": 10000.0,
                    "positions": []
                }

    def save(self):
        with open(self.filepath, "w") as f:
            json.dump(self.data, f, indent=2)

    def get_strategy_stats(self):
        """Computes true wins, losses, and win rates dynamically from trade_history.csv."""
        stats = {strat: {"wins": 0, "losses": 0, "win_rate": 0.0} for strat in STRATEGY_ROSTER}
        if os.path.exists(HISTORY_PATH):
            try:
                df = pd.read_csv(HISTORY_PATH)
                if not df.empty:
                    for strat, group in df.groupby("strategy"):
                        wins = int((group["net_pnl"] > 0).sum())
                        losses = int((group["net_pnl"] <= 0).sum())
                        total = wins + losses
                        win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0
                        stats[strat] = {"wins": wins, "losses": losses, "win_rate": win_rate}
            except Exception as e:
                print(f"[WARN] CSV parse failure for stats: {e}")
        return stats

# =====================================================================
# Google Sheets Synchronization
# =====================================================================

class GoogleSheetsSync:
    def __init__(self, creds_path=GOOGLE_CREDENTIALS_PATH, sheet_name=GOOGLE_SHEET_NAME):
        self.creds_path = creds_path
        self.sheet_name = sheet_name

    def sync(self, portfolio_mgr: PortfolioManager):
        if not GSPREAD_AVAILABLE or not os.path.exists(self.creds_path):
            return

        try:
            gc = gspread.service_account(filename=self.creds_path)
            sh = gc.open(self.sheet_name)

            # Tab 1: Orders and Queues
            ws_orders = sh.worksheet("Orders") if "Orders" in [w.title for w in sh.worksheets()] else sh.sheet1
            ws_orders.clear()
            ws_orders.append_row([
                "Status", "Strategy", "Signal Ticker", "Signal Market", 
                "Action Ticker", "Action Market", "Side", "Qty", 
                "Entry Price", "TP", "SL", "Signal Time", "Action Time", "Pull Trigger Time"
            ])

            for strat_name, strat in portfolio_mgr.data.get("strategies", {}).items():
                for pos in strat.get("positions", []):
                    ws_orders.append_row([
                        pos.get("status"),
                        strat_name,
                        pos.get("signal_ticker"),
                        pos.get("signal_market"),
                        pos.get("action_ticker"),
                        pos.get("action_market"),
                        pos.get("direction"),
                        pos.get("qty"),
                        pos.get("entry_price"),
                        pos.get("tp_price"),
                        pos.get("sl_price"),
                        pos.get("signal_time"),
                        pos.get("action_time", "Pending"),
                        format_trigger_time(pos.get("signal_time"), pos.get("action_time"))
                    ])

            # Tab 2: Strategy Stats & Wins/Losses
            if "Strategy_Stats" in [w.title for w in sh.worksheets()]:
                ws_stats = sh.worksheet("Strategy_Stats")
                ws_stats.clear()
                ws_stats.append_row(["Strategy", "Wins", "Losses", "Win Rate (%)", "Allocated", "Cash"])
                
                stats = portfolio_mgr.get_strategy_stats()
                for strat_name in STRATEGY_ROSTER:
                    s_info = portfolio_mgr.data["strategies"].get(strat_name, {})
                    s_stat = stats.get(strat_name, {"wins": 0, "losses": 0, "win_rate": 0.0})
                    ws_stats.append_row([
                        strat_name,
                        s_stat["wins"],
                        s_stat["losses"],
                        f"{s_stat['win_rate']}%",
                        s_info.get("allocated", 10000.0),
                        s_info.get("cash", 10000.0)
                    ])

        except Exception as e:
            print(f"[ERROR] Google Sheets Sync Error: {e}")

# =====================================================================
# Execution Engine
# =====================================================================

class ExecutionEngine:
    def __init__(self):
        ensure_environment()
        self.portfolio_mgr = PortfolioManager()
        self.gsheet_sync = GoogleSheetsSync()
        start_health_check_server()

    def process_signal(self, signal_payload: dict):
        """Processes incoming catalyst signals, determines queueing, and calculates dynamic TP/SL."""
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
        self.gsheet_sync.sync(self.portfolio_mgr)

        ttt_str = format_trigger_time(signal_time, action_time)
        print(f"[{order_status}] Strategy: {strat_name} | Action: {position_record['action_ticker']} | TP: {tp} | SL: {sl} | Trigger Time: {ttt_str}")

    def process_pending_queues(self):
        """Scans queued market open orders and activates them when exchange opens."""
        updated = False
        for strat_name, strat_info in self.portfolio_mgr.data.get("strategies", {}).items():
            for pos in strat_info.get("positions", []):
                if pos.get("status") == "PENDING_MARKET_OPEN":
                    if is_market_open(pos.get("action_market", "NYSE")):
                        pos["status"] = "ACTIVE"
                        pos["action_time"] = datetime.now(timezone.utc).isoformat()
                        updated = True
                        ttt_str = format_trigger_time(pos["signal_time"], pos["action_time"])
                        print(f"[MARKET OPENED] Activated order for {pos['action_ticker']}. Pull Trigger Time: {ttt_str}")

        if updated:
            self.portfolio_mgr.save()
            self.gsheet_sync.sync(self.portfolio_mgr)

    def close_position(self, strat_name: str, action_ticker: str, exit_price: float, reason: str = "TAKE_PROFIT"):
        """Closes active position and updates trade_history.csv with real PnL."""
        strat_info = self.portfolio_mgr.data["strategies"].get(strat_name)
        if not strat_info:
            return

        remaining = []
        for pos in strat_info.get("positions", []):
            if pos["action_ticker"] == action_ticker and pos["status"] == "ACTIVE":
                qty = pos["qty"]
                entry_price = pos["entry_price"]
                direction = pos.get("direction", "BUY")

                multiplier = 1 if direction == "BUY" else -1
                gross_pnl = round((exit_price - entry_price) * qty * multiplier, 2)
                fee = round(max(1.00, qty * 0.005), 2)
                net_pnl = round(gross_pnl - fee, 2)

                ttt_str = format_trigger_time(pos["signal_time"], pos["action_time"])

                with open(HISTORY_PATH, "a", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        datetime.now(timezone.utc).isoformat(),
                        strat_name,
                        pos.get("signal_ticker"),
                        pos.get("action_ticker"),
                        f"CLOSE_{direction}",
                        qty,
                        exit_price,
                        gross_pnl,
                        fee,
                        net_pnl,
                        ttt_str,
                        reason
                    ])

                strat_info["cash"] = round(strat_info["cash"] + net_pnl, 2)
                print(f"[CLOSED TRADE] {strat_name} ({action_ticker}) | Net PnL: ${net_pnl} | Reason: {reason}")
            else:
                remaining.append(pos)

        strat_info["positions"] = remaining
        self.portfolio_mgr.save()
        self.gsheet_sync.sync(self.portfolio_mgr)

# =====================================================================
# Main Loop Run Simulation
# =====================================================================

if __name__ == "__main__":
    engine = ExecutionEngine()
    create_offline_backup()
    print("🚀 LagTrader Engine Running. Monitoring signals and market hours...")

    # Simulated Incoming Signal Example
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
