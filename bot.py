import os
import sys
import json
import time
import csv
import zipfile
import threading
import base64
import urllib.request
import urllib.parse
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

# TwelveData API Key (fallback price provider)
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "e5412639c4844ff8b877be3f53b69c9d")

# GitHub Persistence Configuration (Optional: auto-commits trade history)
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")    # format: "username/repository"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # GitHub Personal Access Token

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
    "NYSE": (13.5, 20.0),    # 9:30 AM - 4:00 PM US Eastern
    "NASDAQ": (13.5, 20.0),
    "TSX": (13.5, 20.0),     # Toronto
    "XETR": (7.0, 15.5),     # Frankfurt
    "LSE": (7.0, 15.5),      # London
    "OMX": (7.0, 15.0),      # Nordic / Copenhagen
    "TSE": (0.0, 6.5),       # Tokyo (9:00 AM - 3:30 PM JST)
    "KOSPI": (0.0, 6.5),     # Seoul (9:00 AM - 3:30 PM KST)
    "ASX": (23.0, 6.0),      # Sydney (Opens 23:00 UTC Sunday to 06:00 UTC)
    "CME": (0.0, 24.0),      # Futures
    "CRYPTO": (0.0, 24.0)    # 24/7
}

# =====================================================================
# GitHub Automatic State Persistence (Surviving Render Restarts)
# =====================================================================

def pull_file_from_github(file_path: str, repo: str, token: str):
    """Downloads the latest file from GitHub on container boot if missing locally."""
    if not repo or not token:
        return False
    try:
        filename = os.path.basename(file_path)
        repo_path = f"data/{filename}"
        api_url = f"https://api.github.com/repos/{repo}/contents/{repo_path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "LagTrader-Bot"
        }
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content_b64 = data.get("content", "")
            if content_b64:
                file_bytes = base64.b64decode(content_b64)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "wb") as f:
                    f.write(file_bytes)
                print(f"📥 [GITHUB SYNC] Restored latest {filename} from GitHub repository!")
                return True
    except Exception:
        pass
    return False

def sync_file_to_github(file_path: str, repo: str, token: str, commit_msg: str):
    """Automatically commits state files back to GitHub so data is permanently safe."""
    if not repo or not token or not os.path.exists(file_path):
        return False
    try:
        filename = os.path.basename(file_path)
        repo_path = f"data/{filename}"
        api_url = f"https://api.github.com/repos/{repo}/contents/{repo_path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "LagTrader-Bot"
        }

        # Check existing file SHA
        sha = None
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                sha = data.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"[GITHUB SYNC] Error looking up SHA: {e}")

        # Base64 encode file content
        with open(file_path, "rb") as f:
            content_bytes = f.read()
        content_b64 = base64.b64encode(content_bytes).decode("utf-8")

        payload = {"message": commit_msg, "content": content_b64}
        if sha:
            payload["sha"] = sha

        put_req = urllib.request.Request(
            api_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PUT"
        )
        with urllib.request.urlopen(put_req, timeout=10):
            print(f"📦 [GITHUB SYNC] Successfully auto-committed {filename} to GitHub!")
            return True
    except Exception as e:
        print(f"[GITHUB SYNC ERROR] {e}")
        return False

# =====================================================================
# Market Calendar & US Holiday Intelligence
# =====================================================================

def is_us_holiday(d: date) -> bool:
    """Detects US market holidays where NYSE and NASDAQ are closed."""
    if d.month == 1 and d.day == 1:
        return True  # New Year's Day
    if d.month == 1 and d.weekday() == 0 and 15 <= d.day <= 21:
        return True  # MLK Day
    if d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21:
        return True  # Presidents' Day
    if d.month == 5 and d.weekday() == 0 and d.day >= 25:
        return True  # Memorial Day
    if d.month == 6 and d.day == 19:
        return True  # Juneteenth
    if d.month == 7 and d.day == 4:
        return True  # Independence Day
    if d.month == 9 and d.weekday() == 0 and 1 <= d.day <= 7:
        return True  # Labor Day
    if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
        return True  # Thanksgiving
    if d.month == 12 and d.day == 25:
        return True  # Christmas Day
    return False

def is_market_open(market_name: str, now: datetime = None) -> bool:
    """Smart market schedule check (Timezones, Sunday Asian open, and Holidays)."""
    if now is None:
        now = datetime.now(timezone.utc)
    market = (market_name or "NYSE").upper()

    if market == "CRYPTO":
        return True

    weekday = now.weekday()
    utc_hour = now.hour + (now.minute / 60.0)

    # Saturday: All global stock & futures exchanges closed
    if weekday == 5:
        return False

    # Sunday:
    if weekday == 6:
        if market == "CME":
            return utc_hour >= 22.0
        if market == "ASX":
            return utc_hour >= 23.0
        return False

    # Friday CME pause at 21:00 UTC
    if weekday == 4 and market == "CME" and utc_hour >= 21.0:
        return False

    # US Holiday Filter (e.g. Labor Day)
    if market in ["NYSE", "NASDAQ"] and is_us_holiday(now.date()):
        return False

    open_h, close_h = EXCHANGE_HOURS_UTC.get(market, (0.0, 24.0))
    if open_h == 0.0 and close_h == 24.0:
        return True

    if open_h > close_h:
        return utc_hour >= open_h or utc_hour <= close_h

    return open_h <= utc_hour <= close_h

# =====================================================================
# Pricing Engine (Yahoo Finance with TwelveData Backup)
# =====================================================================

def fetch_twelvedata_price(ticker: str, api_key: str):
    """Fallback price lookup via TwelveData API."""
    if not api_key:
        return None
    try:
        clean_sym = ticker.split(".")[0]
        url = f"https://api.twelvedata.com/price?symbol={clean_sym}&apikey={api_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "LagTrader/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "price" in data:
                val = float(data["price"])
                if val > 0:
                    return round(val, 2)
    except Exception:
        pass
    return None

def fetch_live_price(ticker: str):
    """Fetches real-time price using yfinance, falling back to TwelveData."""
    if not ticker:
        return None

    if YFINANCE_AVAILABLE:
        try:
            t = yf.Ticker(ticker)
            price = t.fast_info.get("last_price")
            if price is not None and not pd.isna(price) and price > 0:
                return round(float(price), 2)
            hist = t.history(period="1d", interval="1m")
            if not hist.empty and "Close" in hist:
                last_val = hist["Close"].iloc[-1]
                if not pd.isna(last_val) and last_val > 0:
                    return round(float(last_val), 2)
        except Exception:
            pass

    if TWELVEDATA_API_KEY:
        td_price = fetch_twelvedata_price(ticker, TWELVEDATA_API_KEY)
        if td_price is not None and td_price > 0:
            return td_price

    return None

# =====================================================================
# Utilities
# =====================================================================

def ensure_environment():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Restore from GitHub on boot if available
    if GITHUB_REPO and GITHUB_TOKEN:
        pull_file_from_github(PORTFOLIO_PATH, GITHUB_REPO, GITHUB_TOKEN)
        pull_file_from_github(HISTORY_PATH, GITHUB_REPO, GITHUB_TOKEN)

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
    direction_clean = direction.upper()

    if direction_clean in ["BUY", "LONG"]:
        tp_price = round(entry_price * (1 + (expected_move_pct / 100.0)), 2)
        sl_price = round(entry_price - (1.5 * atr_14), 2)
    else:  # SELL / SHORT
        tp_price = round(entry_price * (1 - (expected_move_pct / 100.0)), 2)
        sl_price = round(entry_price + (1.5 * atr_14), 2)
        
    return tp_price, sl_price

def format_trigger_time(signal_time_iso, action_time_iso=None) -> str:
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
                        pos_copy["signal_ticker"] = pos.get("signal_ticker", "-")
                        pos_copy["signal_market"] = pos.get("signal_market", "-")
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

            recent_trades = []
            if os.path.exists(HISTORY_PATH):
                try:
                    df = pd.read_csv(HISTORY_PATH)
                    if not df.empty:
                        recent_trades = df.tail(5).to_dict(orient="records")
                        recent_trades.reverse()
                except Exception:
                    pass

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
                "recent_trades": recent_trades,
                "strategies": strat_list
            }
        except Exception as e:
            return {
                "status": "online",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kpi": {"total_capital": 240000.0, "total_wins": 0, "total_losses": 0, "win_rate": 0.0},
                "orders": [],
                "recent_trades": [],
                "strategies": [{"name": s, "wins": 0, "losses": 0, "win_rate": 0.0, "allocated": 10000.0, "cash": 10000.0} for s in STRATEGY_ROSTER]
            }

# =====================================================================
# Web API & Webhook Server (GET /api/data & POST /api/signal)
# =====================================================================

ENGINE_INSTANCE = None

class DashboardAPIHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json_response(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

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
                self._send_json_response(200, payload)

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
                    self._send_json_response(404, {"status": "error", "message": "No trade history yet."})

            else:
                self._send_json_response(200, {
                    "status": "online", "system": "LagTrader Engine", "timestamp": datetime.now(timezone.utc).isoformat()
                })

        except Exception as e:
            self._send_json_response(500, {"status": "error", "message": str(e)})

    def do_POST(self):
        """Webhook listener for incoming signals from TradingView, curl, or external bots."""
        try:
            parsed_path = self.path.split("?")[0]
            if parsed_path == "/api/signal":
                content_len = int(self.headers.get("Content-Length", 0))
                if content_len == 0:
                    self._send_json_response(400, {"status": "error", "message": "Empty signal body"})
                    return

                post_data = self.rfile.read(content_len)
                payload = json.loads(post_data.decode("utf-8"))

                if not ENGINE_INSTANCE:
                    self._send_json_response(503, {"status": "error", "message": "Engine starting"})
                    return

                success, msg = ENGINE_INSTANCE.process_signal(payload)
                status_code = 200 if success else 400
                self._send_json_response(status_code, {
                    "status": "accepted" if success else "rejected",
                    "message": msg
                })
            else:
                self._send_json_response(404, {"status": "error", "message": "Endpoint not found"})
        except Exception as e:
            self._send_json_response(500, {"status": "error", "message": str(e)})

    def log_message(self, format, *args):
        return

def start_server(port=HTTP_PORT):
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[SERVER] Listening on port {port} (GET /api/data & POST /api/signal)")

# =====================================================================
# Execution Engine (With Real Cash Accounting)
# =====================================================================

class ExecutionEngine:
    def __init__(self):
        global ENGINE_INSTANCE
        ensure_environment()
        self.portfolio_mgr = PortfolioManager()
        ENGINE_INSTANCE = self
        start_server()

    def process_signal(self, signal_payload: dict):
        """Processes incoming signal with strict Cash & Margin allocation."""
        strat_name = signal_payload.get("strategy")
        if not strat_name or strat_name not in STRATEGY_ROSTER:
            return False, f"Unknown or missing strategy '{strat_name}'"

        action_ticker = signal_payload.get("action_ticker")
        if not action_ticker:
            return False, "Missing 'action_ticker'"

        signal_time = signal_payload.get("signal_time", datetime.now(timezone.utc).isoformat())
        action_market = signal_payload.get("action_market", "NYSE")
        
        market_open = is_market_open(action_market)
        order_status = "ACTIVE" if market_open else "PENDING_MARKET_OPEN"
        action_time = datetime.now(timezone.utc).isoformat() if market_open else None

        entry_price = float(signal_payload.get("entry_price", 100.0))
        discrepancy = float(signal_payload.get("discrepancy_pct", 1.5))
        beta = float(signal_payload.get("beta", 1.0))
        atr_14 = float(signal_payload.get("atr_14", 0.50))
        direction = signal_payload.get("direction", "BUY").upper()
        qty = int(signal_payload.get("qty", 100))

        # 1. Cash & Margin Check: Deduct capital on trade entry
        required_capital = round(qty * entry_price, 2)
        strat_dict = self.portfolio_mgr.data["strategies"].setdefault(
            strat_name, {"allocated": 10000.0, "cash": 10000.0, "positions": []}
        )
        available_cash = strat_dict.get("cash", 10000.0)

        if available_cash < required_capital:
            print(f"❌ [ORDER REJECTED] {strat_name}: Insufficient cash (Required: ${required_capital}, Available: ${available_cash})")
            return False, f"Insufficient cash: Required ${required_capital:.2f}, Available ${available_cash:.2f}"

        # Deduct reserved cash
        strat_dict["cash"] = round(available_cash - required_capital, 2)

        tp, sl = calculate_dynamic_tp_sl(entry_price, discrepancy, beta, atr_14, direction)

        position_record = {
            "order_id": f"ORD_{int(time.time()*1000)}",
            "status": order_status,
            "strategy": strat_name,
            "signal_ticker": signal_payload.get("signal_ticker", "-"),
            "signal_market": signal_payload.get("signal_market", "-"),
            "action_ticker": action_ticker,
            "action_market": action_market,
            "direction": direction,
            "qty": qty,
            "entry_price": entry_price,
            "invested_capital": required_capital,
            "tp_price": tp,
            "sl_price": sl,
            "signal_time": signal_time,
            "action_time": action_time
        }

        strat_dict["positions"].append(position_record)
        self.portfolio_mgr.save()

        ttt_str = format_trigger_time(signal_time, action_time)
        print(f"[{order_status}] {strat_name} | {direction} {action_ticker} ({action_market}) | Cost: ${required_capital} | Cash Left: ${strat_dict['cash']} | TP: {tp} | SL: {sl} | Trigger: {ttt_str}")
        return True, f"Order {position_record['order_id']} placed successfully ({order_status})"

    def process_pending_queues(self):
        """Activates queued orders when Action Market opens, updating entry price to real market open."""
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
                            
                            if real_open_price and real_open_price > 0:
                                old_cost = pos.get("invested_capital", pos["entry_price"] * pos["qty"])
                                new_cost = round(real_open_price * pos["qty"], 2)
                                cost_diff = round(new_cost - old_cost, 2)
                                
                                # Adjust cash for gap opening difference
                                strat_info["cash"] = round(strat_info.get("cash", 0.0) - cost_diff, 2)
                                pos["entry_price"] = real_open_price
                                pos["invested_capital"] = new_cost
                                
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
        """Monitors open positions and exits when TP or SL is reached (only when exchange is open!)."""
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
                        
                        if not is_market_open(action_mkt):
                            continue

                        ticker = pos.get("action_ticker") or pos.get("ticker")
                        if not ticker:
                            continue

                        current_price = fetch_live_price(ticker)
                        if not current_price:
                            continue

                        direction = pos.get("direction", "BUY").upper()
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
                    direction = pos.get("direction", "BUY").upper()

                    multiplier = 1 if direction in ["BUY", "LONG"] else -1
                    gross_pnl = round((exit_price - entry_price) * qty * multiplier, 2)
                    fee = round(max(1.00, qty * 0.005), 2)
                    net_pnl = round(gross_pnl - fee, 2)

                    # Return initial invested capital + profit/loss back to strategy cash
                    invested = pos.get("invested_capital", round(qty * entry_price, 2))
                    returned_total = round(invested + net_pnl, 2)
                    strat_info["cash"] = round(strat_info.get("cash", 0.0) + returned_total, 2)
                    
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

                    print(f"🎯 [TRADE CLOSED] {strat_name} ({direction} {ticker}) at ${exit_price} | Net PnL: ${net_pnl} | Return: ${returned_total} | New Cash: ${strat_info['cash']} | Trigger Time: {ttt_str}")
                else:
                    remaining.append(pos)

            strat_info["positions"] = remaining
        self.portfolio_mgr.save()

        # Auto-sync state back to GitHub repository in the background
        if GITHUB_REPO and GITHUB_TOKEN:
            threading.Thread(
                target=sync_file_to_github,
                args=(HISTORY_PATH, GITHUB_REPO, GITHUB_TOKEN, f"Auto-sync: closed {action_ticker} ({reason})"),
                daemon=True
            ).start()
            threading.Thread(
                target=sync_file_to_github,
                args=(PORTFOLIO_PATH, GITHUB_REPO, GITHUB_TOKEN, f"Auto-sync: portfolio after {action_ticker}"),
                daemon=True
            ).start()

# =====================================================================
# Main Loop (Resource-Efficient 24/7 Engine)
# =====================================================================

if __name__ == "__main__":
    engine = ExecutionEngine()
    print("🚀 LagTrader Engine Running. Smart 24/7 Market Monitor & Webhook Active.")

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
        try:
            engine.process_pending_queues()
            engine.check_active_positions_tp_sl()
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
        time.sleep(60)
