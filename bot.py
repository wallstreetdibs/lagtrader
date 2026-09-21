import os
import sys
import json
import time
import csv
import queue
import threading
import base64
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, date

try:
    import pandas as pd
except ImportError:
    pd = None

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

# TwelveData API Keys (Supports automatic primary & secondary failover rotation)
TWELVEDATA_KEYS = [
    k for k in [
        os.environ.get("TWELVEDATA_KEY"),
        os.environ.get("TWELVEDATA_KEY2"),
        os.environ.get("TWELVEDATA_API_KEY")
    ] if k
]

# GitHub Persistence Configuration
GITHUB_REPO = os.environ.get("GITHUB_REPO", "wallstreetdibs/lagtrader")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Realistic Execution Slippage Buffer (0.05% friction)
SLIPPAGE_PCT = 0.0005

STRATEGY_ROSTER = [
    "ASX_ADR_Arbitrage", "US_Earnings_Lag", "Inventory_Drift_Reversal",
    "Futures_Lead_Front_Run", "Crypto_FinTech_Echo", "Sentiment_Echo",
    "Immediate_Index_Proxy", "Nikkei_ADR_Front_Run", "WTI_Crude_Lag",
    "Gold_Futures_Echo", "Biotech_News_Lag", "Cross_Listed_Pair_Fade",
    "Time_Zone_Momentum_Relay", "FX_Adjusted_Earnings_Arb", "Commodity_Proxy_Lag",
    "ETF_NAV_Window_Arb", "Nikkei_Tech_Relay", "London_Metals_Catchup",
    "Treasury_Shockwave", "Canadian_Energy_Echo", "ETF_Creation_Lag",
    "SKHY_ADR_FX_Neutralization", "SKHY_HBM_Supply_Chain", "SKHY_Post_Market_KOSPI",
    "TSMC_ADR_Arbitrage", "EUV_Lithography_Echo", "GLP1_Duopoly_Relay",
    "Crypto_Weekend_Gap_Run", "SoftBank_ARM_Nexus"
]

EXCHANGE_HOURS_UTC = {
    "NYSE": (13.5, 20.0),    # 9:30 AM - 4:00 PM US Eastern
    "NASDAQ": (13.5, 20.0),
    "TSX": (13.5, 20.0),
    "XETR": (7.0, 15.5),
    "LSE": (7.0, 15.5),
    "AMS": (7.0, 15.5),
    "OMX": (7.0, 15.0),
    "TSE": (0.0, 6.5),
    "KOSPI": (0.0, 6.5),
    "TWSE": (1.0, 5.5),
    "ASX": (23.0, 6.0),
    "CME": (0.0, 24.0),
    "CRYPTO": (0.0, 24.0)
}

ENTRY_HOURS_UTC = {
    "NYSE": (13.75, 19.75),   # 9:45 AM - 3:45 PM US Eastern
    "NASDAQ": (13.75, 19.75),
    "TSX": (13.75, 19.75),
    "XETR": (7.25, 15.25),
    "LSE": (7.25, 15.25),
    "AMS": (7.25, 15.25),
    "OMX": (7.25, 14.75),
    "TSE": (0.25, 6.25),
    "KOSPI": (0.25, 6.25),
    "TWSE": (1.25, 5.25),
    "ASX": (23.25, 5.75),
    "CME": (0.0, 24.0),
    "CRYPTO": (0.0, 24.0)
}

# Thread lock for file and CSV system access
CSV_LOCK = threading.Lock()

# =====================================================================
# Sequential GitHub Persistence Worker (Prevents API Commit Race Conditions)
# =====================================================================

class GitHubSyncWorker:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.token = token
        self.queue = queue.Queue()
        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        if self.repo and self.token:
            self.worker_thread.start()

    def enqueue_sync(self, file_path: str, commit_msg: str):
        if not self.repo or not self.token:
            return
        self.queue.put((file_path, commit_msg))

    def _process_queue(self):
        while True:
            file_path, commit_msg = self.queue.get()
            try:
                self._sync_file_now(file_path, commit_msg)
            except Exception as e:
                print(f"[GITHUB SYNC ERROR] {e}")
            finally:
                self.queue.task_done()

    def _sync_file_now(self, file_path: str, commit_msg: str):
        if not os.path.exists(file_path):
            return
        filename = os.path.basename(file_path)
        repo_path = f"data/{filename}"
        api_url = f"https://api.github.com/repos/{self.repo}/contents/{repo_path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "LagTrader-Bot"
        }

        sha = None
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                sha = data.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"[GITHUB SYNC] SHA lookup error: {e}")

        with CSV_LOCK:
            with open(file_path, "rb") as f:
                content_bytes = f.read()

        content_b64 = base64.b64encode(content_bytes).decode("utf-8")
        payload = {"message": commit_msg, "content": content_b64}
        if sha:
            payload["sha"] = sha

        put_req = urllib.request.Request(
            api_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PUT"
        )
        with urllib.request.urlopen(put_req, timeout=12):
            print(f"📦 [GITHUB SYNC] Auto-committed {filename} successfully.")

def pull_file_from_github(file_path: str, repo: str, token: str):
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content_b64 = data.get("content", "")
            if content_b64:
                file_bytes = base64.b64decode(content_b64)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "wb") as f:
                    f.write(file_bytes)
                print(f"📥 [GITHUB SYNC] Restored {filename} from remote repository!")
                return True
    except Exception:
        pass
    return False

GITHUB_WORKER = GitHubSyncWorker(GITHUB_REPO, GITHUB_TOKEN)

# =====================================================================
# Market Calendar & US Holiday Intelligence
# =====================================================================

def is_us_holiday(d: date) -> bool:
    if d.month == 1 and d.day == 1:
        return True
    if d.month == 1 and d.weekday() == 0 and 15 <= d.day <= 21:
        return True
    if d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21:
        return True
    if d.month == 5 and d.weekday() == 0 and d.day >= 25:
        return True
    if d.month == 6 and d.day == 19:
        return True
    if d.month == 7 and d.day == 4:
        return True
    if d.month == 9 and d.weekday() == 0 and 1 <= d.day <= 7:
        return True
    if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
        return True
    if d.month == 12 and d.day == 25:
        return True
    return False

def is_market_open(market_name: str, for_entry: bool = False, now: datetime = None) -> bool:
    if now is None:
        now = datetime.now(timezone.utc)
    market = (market_name or "NYSE").upper()

    if market == "CRYPTO":
        return True

    weekday = now.weekday()
    utc_hour = now.hour + (now.minute / 60.0)

    # Saturday
    if weekday == 5:
        return False

    # Sunday checks
    if weekday == 6:
        if market == "CME":
            return utc_hour >= 22.0
        if market == "ASX":
            return utc_hour >= 23.0
        return False

    # Friday evening CME closure
    if weekday == 4 and market == "CME" and utc_hour >= 21.0:
        return False

    # Friday evening ASX closure (Sydney Saturday Morning)
    if weekday == 4 and market == "ASX" and utc_hour >= 21.0:
        return False

    if market in ["NYSE", "NASDAQ"] and is_us_holiday(now.date()):
        return False

    schedule = ENTRY_HOURS_UTC if for_entry else EXCHANGE_HOURS_UTC
    open_h, close_h = schedule.get(market, (0.0, 24.0))

    if open_h == 0.0 and close_h == 24.0:
        return True

    if open_h > close_h:
        return utc_hour >= open_h or utc_hour <= close_h

    return open_h <= utc_hour <= close_h

# =====================================================================
# Pricing & TwelveData Key Rotation Engine
# =====================================================================

def fetch_twelvedata_price(ticker: str):
    if not TWELVEDATA_KEYS or not ticker:
        return None

    # Preserve exchange extensions for non-US symbols
    td_ticker = ticker.replace("=", "/").strip()
    for api_key in TWELVEDATA_KEYS:
        try:
            url = f"https://api.twelvedata.com/price?symbol={urllib.parse.quote(td_ticker)}&apikey={api_key}"
            req = urllib.request.Request(url, headers={"User-Agent": "LagTrader/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "price" in data:
                    val = float(data["price"])
                    if val > 0:
                        return round(val, 2)
                elif data.get("code") == 429:
                    continue
        except Exception:
            continue
    return None

def fetch_live_price(ticker: str):
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

    td_price = fetch_twelvedata_price(ticker)
    if td_price is not None and td_price > 0:
        return td_price

    return None

def fetch_intraday_ohlc(ticker: str):
    if YFINANCE_AVAILABLE and ticker:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="1d", interval="5m")
            if not hist.empty and all(col in hist.columns for col in ["Open", "High", "Low", "Close"]):
                latest = hist.iloc[-1]
                return {
                    "open": float(latest["Open"]),
                    "high": float(latest["High"]),
                    "low": float(latest["Low"]),
                    "close": float(latest["Close"])
                }
        except Exception:
            pass

    p = fetch_live_price(ticker)
    if p:
        return {"open": p, "high": p, "low": p, "close": p}
    return None

# =====================================================================
# Utilities
# =====================================================================

def ensure_environment():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    if GITHUB_REPO and GITHUB_TOKEN:
        pull_file_from_github(PORTFOLIO_PATH, GITHUB_REPO, GITHUB_TOKEN)
        pull_file_from_github(HISTORY_PATH, GITHUB_REPO, GITHUB_TOKEN)

    if not os.path.exists(HISTORY_PATH):
        with CSV_LOCK:
            with open(HISTORY_PATH, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "strategy", "signal_ticker", "action_ticker",
                    "action", "qty", "price", "gross_pnl", "fee", "net_pnl",
                    "trigger_time", "reason"
                ])
        GITHUB_WORKER.enqueue_sync(HISTORY_PATH, "Initial trade history creation")

def calculate_dynamic_tp_sl(entry_price: float, signal_discrepancy_pct: float, beta: float = 1.0, atr_14: float = 0.50, direction: str = "BUY"):
    direction_clean = direction.upper()
    effective_atr = max(atr_14, entry_price * 0.015)
    expected_move_pct = max(abs(signal_discrepancy_pct) * beta * 0.80, 1.50)

    sl_distance = round(2.0 * effective_atr, 2)
    tp_distance = round(entry_price * (expected_move_pct / 100.0), 2)

    if direction_clean in ["BUY", "LONG"]:
        tp_price = round(entry_price + tp_distance, 2)
        sl_price = round(entry_price - sl_distance, 2)
    else:
        tp_price = round(entry_price - tp_distance, 2)
        sl_price = round(entry_price + sl_distance, 2)

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
        with self.lock:
            if os.path.exists(self.filepath):
                try:
                    with open(self.filepath, "r") as f:
                        data = json.load(f)
                        self._reconcile_roster(data)
                        return data
                except Exception as e:
                    print(f"[WARN] Failed to parse portfolio JSON: {e}. Resetting to default state.")
            
            default_state = self._build_default_state()
            self._save_unlocked(default_state)
            GITHUB_WORKER.enqueue_sync(self.filepath, "Initialize portfolio state")
            return default_state

    def _build_default_state(self):
        state = {"total_capital": float(len(STRATEGY_ROSTER) * 10000.0), "strategies": {}}
        for strat in STRATEGY_ROSTER:
            state["strategies"][strat] = {"allocated": 10000.0, "cash": 10000.0, "positions": []}
        return state

    def _reconcile_roster(self, data):
        if "strategies" not in data or not isinstance(data["strategies"], dict):
            data["strategies"] = {}
        for strat in STRATEGY_ROSTER:
            if strat not in data["strategies"] or not isinstance(data["strategies"][strat], dict):
                data["strategies"][strat] = {"allocated": 10000.0, "cash": 10000.0, "positions": []}

    def _save_unlocked(self, data_to_save=None):
        payload = data_to_save or self.data
        with open(self.filepath, "w") as f:
            json.dump(payload, f, indent=2)

    def save(self):
        with self.lock:
            self._save_unlocked()

    def get_strategy_stats(self):
        stats = {strat: {"wins": 0, "losses": 0, "win_rate": 0.0, "realized_pnl": 0.0} for strat in STRATEGY_ROSTER}
        if os.path.exists(HISTORY_PATH):
            with CSV_LOCK:
                try:
                    if pd is not None:
                        df = pd.read_csv(HISTORY_PATH)
                        if not df.empty and "strategy" in df.columns and "net_pnl" in df.columns:
                            for strat, group in df.groupby("strategy"):
                                wins = int((group["net_pnl"] > 0).sum())
                                losses = int((group["net_pnl"] <= 0).sum())
                                total_pnl = float(group["net_pnl"].sum())
                                total = wins + losses
                                win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0
                                stats[strat] = {
                                    "wins": wins,
                                    "losses": losses,
                                    "win_rate": win_rate,
                                    "realized_pnl": round(total_pnl, 2)
                                }
                except Exception as e:
                    print(f"[WARN] CSV parse error: {e}")
        return stats

    def get_dashboard_payload(self):
        with self.lock:
            try:
                stats = self.get_strategy_stats()
                total_wins = sum(s["wins"] for s in stats.values())
                total_losses = sum(s["losses"] for s in stats.values())
                total_trades = total_wins + total_losses
                overall_win_rate = round((total_wins / total_trades) * 100, 1) if total_trades > 0 else 0.0

                all_orders = []
                strategies_data = self.data.get("strategies", {})
                
                dynamic_equity = 0.0

                for strat_name, strat in strategies_data.items():
                    if not isinstance(strat, dict):
                        continue
                    cash = float(strat.get("cash", 10000.0))
                    dynamic_equity += cash

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
                        
                        dynamic_equity += float(pos.get("invested_capital", pos_copy["entry_price"] * pos_copy["qty"]))

                strat_list = []
                for s in STRATEGY_ROSTER:
                    s_dict = strategies_data.get(s, {})
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
                    with CSV_LOCK:
                        try:
                            if pd is not None:
                                df = pd.read_csv(HISTORY_PATH)
                                if not df.empty:
                                    recent_trades = df.tail(10).to_dict(orient="records")
                                    recent_trades.reverse()
                        except Exception:
                            pass

                return {
                    "status": "online",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "kpi": {
                        "total_capital": round(dynamic_equity, 2),
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
                    "kpi": {"total_capital": 290000.0, "total_wins": 0, "total_losses": 0, "win_rate": 0.0},
                    "orders": [],
                    "recent_trades": [],
                    "strategies": [{"name": s, "wins": 0, "losses": 0, "win_rate": 0.0, "allocated": 10000.0, "cash": 10000.0} for s in STRATEGY_ROSTER]
                }

# =====================================================================
# Web API Server
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
                    with CSV_LOCK:
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
                    self._send_json_response(404, {"status": "error", "message": "No trade history available."})

            else:
                self._send_json_response(200, {
                    "status": "online", "system": "LagTrader Engine", "timestamp": datetime.now(timezone.utc).isoformat()
                })

        except Exception as e:
            self._send_json_response(500, {"status": "error", "message": str(e)})

    def do_POST(self):
        try:
            parsed_path = self.path.split("?")[0]
            if parsed_path == "/api/signal":
                content_len = int(self.headers.get("Content-Length", 0))
                if content_len == 0:
                    self._send_json_response(400, {"status": "error", "message": "Empty signal payload"})
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
    print(f"[SERVER] Active on port {port} (GET /api/data & POST /api/signal)")

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
        strat_name = signal_payload.get("strategy")
        if not strat_name or strat_name not in STRATEGY_ROSTER:
            return False, f"Unknown or invalid strategy '{strat_name}'"

        action_ticker = signal_payload.get("action_ticker")
        if not action_ticker:
            return False, "Missing required parameter 'action_ticker'"

        signal_time = signal_payload.get("signal_time", datetime.now(timezone.utc).isoformat())
        action_market = signal_payload.get("action_market", "NYSE")

        entry_window_open = is_market_open(action_market, for_entry=True)
        order_status = "ACTIVE" if entry_window_open else "PENDING_MARKET_OPEN"
        action_time = datetime.now(timezone.utc).isoformat() if entry_window_open else None

        raw_entry = float(signal_payload.get("entry_price", 100.0))
        direction = signal_payload.get("direction", "BUY").upper()

        if direction in ["BUY", "LONG"]:
            entry_price = round(raw_entry * (1.0 + SLIPPAGE_PCT), 2)
        else:
            entry_price = round(raw_entry * (1.0 - SLIPPAGE_PCT), 2)

        discrepancy = float(signal_payload.get("discrepancy_pct", 1.5))
        beta = float(signal_payload.get("beta", 1.0))
        atr_14 = float(signal_payload.get("atr_14", 0.50))
        qty = int(signal_payload.get("qty", 100))

        required_capital = round(qty * entry_price, 2)

        with self.portfolio_mgr.lock:
            strat_dict = self.portfolio_mgr.data["strategies"].setdefault(
                strat_name, {"allocated": 10000.0, "cash": 10000.0, "positions": []}
            )
            available_cash = strat_dict.get("cash", 10000.0)

            if available_cash < required_capital:
                return False, f"Insufficient strategy cash: Required ${required_capital:.2f}, Available ${available_cash:.2f}"

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
                "discrepancy_pct": discrepancy,
                "beta": beta,
                "atr_14": atr_14,
                "tp_price": tp,
                "sl_price": sl,
                "signal_time": signal_time,
                "action_time": action_time
            }

            strat_dict["positions"].append(position_record)
            self.portfolio_mgr._save_unlocked()

        GITHUB_WORKER.enqueue_sync(PORTFOLIO_PATH, f"Order placed: {position_record['order_id']}")
        return True, f"Order {position_record['order_id']} placed ({order_status})"

    def process_pending_queues(self):
        updated = False
        with self.portfolio_mgr.lock:
            for strat_name, strat_info in self.portfolio_mgr.data.get("strategies", {}).items():
                if not isinstance(strat_info, dict):
                    continue
                remaining_positions = []
                for pos in strat_info.get("positions", []):
                    if not isinstance(pos, dict):
                        continue

                    if pos.get("status") == "PENDING_MARKET_OPEN":
                        action_mkt = pos.get("action_market", "NYSE")

                        if is_market_open(action_mkt, for_entry=True):
                            ticker = pos.get("action_ticker")
                            direction = pos.get("direction", "BUY").upper()
                            ohlc = fetch_intraday_ohlc(ticker)

                            if ohlc and ohlc["open"] > 0:
                                current_open_price = ohlc["open"]
                                old_entry = pos.get("entry_price", current_open_price)

                                is_trap = False
                                if direction in ["BUY", "LONG"] and current_open_price < old_entry * 0.985:
                                    is_trap = True
                                elif direction in ["SELL", "SHORT"] and current_open_price > old_entry * 1.015:
                                    is_trap = True

                                if is_trap:
                                    refund = pos.get("invested_capital", old_entry * pos["qty"])
                                    strat_info["cash"] = round(strat_info.get("cash", 0.0) + refund, 2)
                                    updated = True
                                    continue

                                if direction in ["BUY", "LONG"]:
                                    executed_price = round(current_open_price * (1.0 + SLIPPAGE_PCT), 2)
                                else:
                                    executed_price = round(current_open_price * (1.0 - SLIPPAGE_PCT), 2)

                                old_cost = pos.get("invested_capital", old_entry * pos["qty"])
                                new_cost = round(executed_price * pos["qty"], 2)
                                cost_diff = round(new_cost - old_cost, 2)

                                strat_info["cash"] = round(strat_info.get("cash", 0.0) - cost_diff, 2)
                                pos["entry_price"] = executed_price
                                pos["invested_capital"] = new_cost

                                disc = pos.get("discrepancy_pct", 1.5)
                                beta = pos.get("beta", 1.0)
                                atr = pos.get("atr_14", 0.50)
                                tp, sl = calculate_dynamic_tp_sl(executed_price, disc, beta, atr, direction)
                                pos["tp_price"] = tp
                                pos["sl_price"] = sl

                                pos["status"] = "ACTIVE"
                                pos["action_time"] = datetime.now(timezone.utc).isoformat()
                                updated = True

                    remaining_positions.append(pos)
                strat_info["positions"] = remaining_positions

            if updated:
                self.portfolio_mgr._save_unlocked()

        if updated:
            GITHUB_WORKER.enqueue_sync(PORTFOLIO_PATH, "Activated pending market orders")

    def check_active_positions_tp_sl(self):
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

                        if not is_market_open(action_mkt, for_entry=False):
                            continue

                        ticker = pos.get("action_ticker") or pos.get("ticker")
                        if not ticker:
                            continue

                        live_price = fetch_live_price(ticker)
                        if live_price is None or live_price <= 0:
                            continue

                        direction = pos.get("direction", "BUY").upper()
                        tp = pos.get("tp_price", 999999)
                        sl = pos.get("sl_price", 0)

                        if direction in ["BUY", "LONG"]:
                            if live_price <= sl:
                                exit_price = round(live_price * (1.0 - SLIPPAGE_PCT), 2)
                                positions_to_close.append((strat_name, ticker, exit_price, "STOP_LOSS"))
                            elif live_price >= tp:
                                exit_price = round(live_price * (1.0 - SLIPPAGE_PCT), 2)
                                positions_to_close.append((strat_name, ticker, exit_price, "TAKE_PROFIT"))
                        else:
                            if live_price >= sl:
                                exit_price = round(live_price * (1.0 + SLIPPAGE_PCT), 2)
                                positions_to_close.append((strat_name, ticker, exit_price, "STOP_LOSS"))
                            elif live_price <= tp:
                                exit_price = round(live_price * (1.0 + SLIPPAGE_PCT), 2)
                                positions_to_close.append((strat_name, ticker, exit_price, "TAKE_PROFIT"))

        for strat_name, ticker, exit_price, reason in positions_to_close:
            self.close_position(strat_name, ticker, exit_price, reason)

    def close_position(self, strat_name: str, action_ticker: str, exit_price: float, reason: str = "TAKE_PROFIT"):
        with self.portfolio_mgr.lock:
            strat_info = self.portfolio_mgr.data["strategies"].get(strat_name)
            if not strat_info:
                return

            remaining = []
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

                    invested = pos.get("invested_capital", round(qty * entry_price, 2))
                    returned_total = round(invested + net_pnl, 2)
                    strat_info["cash"] = round(strat_info.get("cash", 0.0) + returned_total, 2)

                    sig_time = pos.get("signal_time") or pos.get("entry_time")
                    act_time = pos.get("action_time")
                    ttt_str = format_trigger_time(sig_time, act_time)

                    with CSV_LOCK:
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

                    print(f"🎯 [TRADE CLOSED] {strat_name} ({direction} {ticker}) at ${exit_price} | Net PnL:${net_pnl}")
                else:
                    remaining.append(pos)

            strat_info["positions"] = remaining
            self.portfolio_mgr._save_unlocked()

        GITHUB_WORKER.enqueue_sync(HISTORY_PATH, f"Closed {action_ticker} ({reason})")
        GITHUB_WORKER.enqueue_sync(PORTFOLIO_PATH, f"Portfolio update after closing {action_ticker}")

# =====================================================================
# Main Execution Loop
# =====================================================================

if __name__ == "__main__":
    engine = ExecutionEngine()
    print("🚀 LagTrader Engine Running (29 Models). Sequential GitHub Persistence & Thread-Safe Core Active.")

    while True:
        try:
            engine.process_pending_queues()
            engine.check_active_positions_tp_sl()
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
        time.sleep(60)
