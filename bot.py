import os
import json
import time
import csv
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
PORTFOLIO_PATH = os.path.join(DATA_DIR, "portfolio_state.json")
HISTORY_PATH = os.path.join(DATA_DIR, "trade_history.csv")
GOOGLE_CREDENTIALS_PATH = "google_credentials.json"
GOOGLE_SHEET_NAME = "LagTrader_Dashboard_Data"

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
    "XCSE": (8.0, 16.0),
    "TSE": (0.0, 6.0),
    "OSE": (0.0, 6.0),
    "ASX": (0.0, 6.0),
    "KOSPI": (0.0, 6.5),
    "CME": (0.0, 24.0),
    "CRYPTO": (0.0, 24.0)
}

# =====================================================================
# Helper Utility Functions
# =====================================================================

def ensure_environment():
    """Ensure data storage directory and history CSV exist."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "strategy", "signal_ticker", "action_ticker",
                "action", "qty", "price", "gross_pnl", "fee", "net_pnl",
                "latency_ms", "reason"
            ])

def is_market_open(market_name: str) -> bool:
    """Checks whether the specified target market is currently open in UTC."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5 and market_name not in ["CME", "CRYPTO"]:
        return False
    
    open_h, close_h = EXCHANGE_HOURS_UTC.get(market_name.upper(), (0.0, 24.0))
    if open_h == 0.0 and close_h == 24.0:
        return True
    
    utc_hour = now.hour + (now.minute / 60.0)
    return open_h <= utc_hour <= close_h

def compute_latency_ms(signal_iso: str, action_iso: str) -> float:
    """Calculates execution latency in milliseconds between signal and order placement."""
    t_sig = datetime.fromisoformat(signal_iso)
    t_act = datetime.fromisoformat(action_iso)
    return round((t_act - t_sig).total_seconds() * 1000, 2)

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
                print(f"[WARN] Error loading portfolio state: {e}. Reinitializing.")
        
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
        """Ensures all 24 strategies exist in the json file."""
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
        """Aggregates wins, losses, and win rate per strategy from history CSV."""
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
                print(f"[WARN] Failed to compute strategy stats from CSV: {e}")
        return stats

# =====================================================================
# Google Sheets Integrator
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

            # --- Sheet 1: Active & Queued Orders ---
            ws_orders = sh.worksheet("Orders") if "Orders" in [w.title for w in sh.worksheets()] else sh.sheet1
            ws_orders.clear()
            ws_orders.append_row([
                "Status", "Strategy", "Signal Ticker", "Signal Market", 
                "Action Ticker", "Action Market", "Side", "Qty", 
                "Entry Price", "Signal Time", "Action Time", "Latency (ms)"
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
                        pos.get("signal_time"),
                        pos.get("action_time"),
                        pos.get("latency_ms")
                    ])

            # --- Sheet 2: Strategy Roster & Win/Loss Counts ---
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
            print(f"[ERROR] Google Sheets Sync Failed: {e}")

# =====================================================================
# Execution Engine
# =====================================================================

class ExecutionEngine:
    def __init__(self):
        ensure_environment()
        self.portfolio_mgr = PortfolioManager()
        self.gsheet_sync = GoogleSheetsSync()

    def process_signal(self, signal_payload: dict):
        """
        Receives signal payload, evaluates exchange open/close state,
        computes signal-to-action latency, and logs order status.
        """
        signal_time = signal_payload.get("signal_time", datetime.now(timezone.utc).isoformat())
        action_time = datetime.now(timezone.utc).isoformat()
        latency_ms = compute_latency_ms(signal_time, action_time)

        strat_name = signal_payload["strategy"]
        action_market = signal_payload.get("action_market", "NYSE")
        
        # Determine initial order status based on target market hours
        order_status = "ACTIVE" if is_market_open(action_market) else "PENDING_MARKET_OPEN"

        position_record = {
            "order_id": f"ORD_{int(time.time()*1000)}",
            "status": order_status,
            "signal_ticker": signal_payload.get("signal_ticker"),
            "signal_market": signal_payload.get("signal_market"),
            "action_ticker": signal_payload.get("action_ticker"),
            "action_market": action_market,
            "direction": signal_payload.get("direction", "BUY"),
            "qty": signal_payload.get("qty", 100),
            "entry_price": signal_payload.get("entry_price", 0.0),
            "tp_price": signal_payload.get("tp_price", 0.0),
            "sl_price": signal_payload.get("sl_price", 0.0),
            "signal_time": signal_time,
            "action_time": action_time,
            "latency_ms": latency_ms
        }

        # Append to strategy positions state
        strat_dict = self.portfolio_mgr.data["strategies"].setdefault(
            strat_name, {"allocated": 10000.0, "cash": 10000.0, "positions": []}
        )
        strat_dict["positions"].append(position_record)
        
        self.portfolio_mgr.save()
        self.gsheet_sync.sync(self.portfolio_mgr)

        print(f"[{order_status}] Strategy: {strat_name} | Action Ticker: {position_record['action_ticker']} | Latency: {latency_ms}ms")

    def process_pending_orders(self):
        """Re-evaluates queued market orders when exchange sessions open."""
        updated = False
        for strat_name, strat_info in self.portfolio_mgr.data.get("strategies", {}).items():
            for pos in strat_info.get("positions", []):
                if pos.get("status") == "PENDING_MARKET_OPEN":
                    if is_market_open(pos.get("action_market", "NYSE")):
                        pos["status"] = "ACTIVE"
                        pos["action_time"] = datetime.now(timezone.utc).isoformat()
                        pos["latency_ms"] = compute_latency_ms(pos["signal_time"], pos["action_time"])
                        updated = True
                        print(f"[ORDER ACTIVATED] {strat_name} order for {pos['action_ticker']} is now ACTIVE.")
        
        if updated:
            self.portfolio_mgr.save()
            self.gsheet_sync.sync(self.portfolio_mgr)

    def close_position(self, strat_name: str, action_ticker: str, exit_price: float, reason: str = "TAKE_PROFIT"):
        """Closes an active position and logs net PnL and trade statistics to CSV."""
        strat_info = self.portfolio_mgr.data["strategies"].get(strat_name)
        if not strat_info:
            return

        remaining_positions = []
        for pos in strat_info.get("positions", []):
            if pos["action_ticker"] == action_ticker and pos["status"] == "ACTIVE":
                qty = pos["qty"]
                entry_price = pos["entry_price"]
                direction = pos.get("direction", "BUY")

                # PnL Calculation
                multiplier = 1 if direction == "BUY" else -1
                gross_pnl = round((exit_price - entry_price) * qty * multiplier, 2)
                fee = round(max(1.00, qty * 0.005), 2)  # Broker fee estimate
                net_pnl = round(gross_pnl - fee, 2)

                # Append to trade_history.csv
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
                        pos.get("latency_ms", 0.0),
                        reason
                    ])

                # Update strategy cash balance
                strat_info["cash"] = round(strat_info["cash"] + net_pnl, 2)
                print(f"[CLOSED] {strat_name} on {action_ticker} | Net PnL: ${net_pnl} | Reason: {reason}")
            else:
                remaining_positions.append(pos)

        strat_info["positions"] = remaining_positions
        self.portfolio_mgr.save()
        self.gsheet_sync.sync(self.portfolio_mgr)

# =====================================================================
# Main Loop Run Engine
# =====================================================================

if __name__ == "__main__":
    engine = ExecutionEngine()
    print("🚀 LagTrader Bot Execution Engine Running...")

    # Sample execution cycle simulation for test validation
    sample_signal = {
        "strategy": "SKHY_ADR_FX_Neutralization",
        "signal_ticker": "000660.KS",
        "signal_market": "KOSPI",
        "action_ticker": "HXSCL",
        "action_market": "NYSE",
        "direction": "BUY",
        "qty": 200,
        "entry_price": 18.45,
        "tp_price": 19.20,
        "sl_price": 18.00,
        "signal_time": datetime.now(timezone.utc).isoformat()
    }

    engine.process_signal(sample_signal)
    engine.process_pending_orders()
