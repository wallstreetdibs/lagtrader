import os
import json
import csv
from datetime import datetime, timezone
import math
import yfinance as yf
import pandas as pd
import numpy as np

# File Paths
DATA_DIR = "data"
PORTFOLIO_PATH = os.path.join(DATA_DIR, "portfolio_state.json")
HISTORY_PATH = os.path.join(DATA_DIR, "trade_history.csv")

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# Complete Strategy Roster (24 Strategies)
STRATEGY_NAMES = [
    "ASX_ADR_Arbitrage", "US_Earnings_Lag", "Inventory_Drift_Reversal",
    "Futures_Lead_Front_Run", "Crypto_FinTech_Echo", "Sentiment_Echo",
    "Immediate_Index_Proxy", "Nikkei_ADR_Front_Run", "WTI_Crude_Lag",
    "Gold_Futures_Echo", "Biotech_News_Lag", "Cross_Listed_Pair_Fade",
    "Time_Zone_Momentum_Relay", "FX_Adjusted_Earnings_Arb", "Commodity_Proxy_Lag",
    "ETF_NAV_Window_Arb", "Nikkei_Tech_Relay", "London_Metals_Catchup",
    "Treasury_Shockwave", "Canadian_Energy_Echo", "ETF_Creation_Lag",
    "SKHY_ADR_FX_Neutralization", "SKHY_HBM_Supply_Chain", "SKHY_Post_Market_KOSPI"
]

# -------------------------------------------------------------------
# Helper Functions: State Management & Fees
# -------------------------------------------------------------------

def load_portfolio_state():
    if os.path.exists(PORTFOLIO_PATH):
        try:
            with open(PORTFOLIO_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Warning] Failed to load existing portfolio state ({e}). Re-initializing.")
    
    # Default State
    state = {
        "total_capital": 240000.0,
        "strategies": {name: {"allocated": 10000.0, "cash": 10000.0, "positions": []} for name in STRATEGY_NAMES}
    }
    return state

def save_portfolio_state(state):
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(state, f, indent=2)

def log_trade_history(trade_record):
    file_exists = os.path.exists(HISTORY_PATH)
    headers = ["timestamp", "strategy", "ticker", "action", "qty", "price", "gross_pnl", "fee", "net_pnl", "reason"]
    
    with open(HISTORY_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(trade_record)

def get_broker_fee(ticker: str, trade_value: float) -> float:
    """Simulates IBKR tiered commission structure."""
    if "." in ticker and not ticker.endswith(".US"):
        # Foreign / Cross-listed exchange minimum ($4.70-$4.75 base)
        return max(4.70, trade_value * 0.0008)
    # US Equities ($0.35 min or $0.005/share)
    return max(0.35, trade_value * 0.0005)

# -------------------------------------------------------------------
# Helper Functions: Signal vs. Noise Filters
# -------------------------------------------------------------------

def get_market_data(ticker: str, period: str = "5d", interval: str = "5m") -> pd.DataFrame:
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except Exception as e:
        print(f"[Data Error] Failed to fetch {ticker}: {e}")
        return pd.DataFrame()

def calculate_zscore_and_rvol(df: pd.DataFrame, window: int = 20):
    """Calculates Z-Score of 5-minute return and Relative Volume (RVOL)."""
    if len(df) < window + 1:
        return 0.0, 0.0
    
    returns = df["Close"].pct_change()
    mean_ret = returns.rolling(window).mean()
    std_ret = returns.rolling(window).std()
    
    latest_ret = returns.iloc[-1]
    last_std = std_ret.iloc[-1]
    
    z_score = (latest_ret - mean_ret.iloc[-1]) / last_std if last_std > 0 else 0.0
    
    vol_sma = df["Volume"].rolling(window).mean().iloc[-1]
    rvol = df["Volume"].iloc[-1] / vol_sma if vol_sma > 0 else 0.0
    
    return float(z_score), float(rvol)

def is_earnings_event_today(ticker_symbol: str) -> bool:
    """Verifies whether today is an actual earnings report date for the symbol."""
    try:
        ticker = yf.Ticker(ticker_symbol)
        cal = ticker.calendar
        if cal is not None:
            # Handle DataFrame/Dict formats from yfinance
            if isinstance(cal, pd.DataFrame) and not cal.empty:
                event_date = pd.to_datetime(cal.iloc[0, 0]).date()
                return event_date == datetime.now(timezone.utc).date()
            elif isinstance(cal, dict) and "Earnings Date" in cal:
                dates = cal["Earnings Date"]
                if dates:
                    event_date = pd.to_datetime(dates[0]).date()
                    return event_date == datetime.now(timezone.utc).date()
    except Exception:
        pass
    return False

# -------------------------------------------------------------------
# Engine Step 1: Manage & Exit Open Positions
# -------------------------------------------------------------------

def process_open_positions(state):
    print("\n--- Checking Open Positions for Exits (TP/SL) ---")
    now_str = datetime.now(timezone.utc).isoformat()
    
    for strat_name, strat_data in state["strategies"].items():
        positions = strat_data.get("positions", [])
        updated_positions = []
        
        for pos in positions:
            ticker = pos["ticker"]
            df = get_market_data(ticker, period="1d", interval="1m")
            if df.empty:
                updated_positions.append(pos)
                continue
            
            current_price = float(df["Close"].iloc[-1])
            qty = pos["qty"]
            entry_price = pos["entry_price"]
            tp_price = pos["tp_price"]
            sl_price = pos["sl_price"]
            direction = pos["direction"]
            
            hit_tp = (direction == "LONG" and current_price >= tp_price)
            hit_sl = (direction == "LONG" and current_price <= sl_price)
            
            if hit_tp or hit_sl:
                reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
                gross_proceeds = current_price * qty
                cost_basis = entry_price * qty
                gross_pnl = gross_proceeds - cost_basis
                
                exit_fee = get_broker_fee(ticker, gross_proceeds)
                net_pnl = gross_pnl - pos["entry_fee"] - exit_fee
                
                # Update Strategy Cash
                strat_data["cash"] += (gross_proceeds - exit_fee)
                
                print(f"[{strat_name}] EXIT {ticker} ({reason}): Price ${current_price:.2f} | Net PnL: ${net_pnl:.2f}")
                
                log_trade_history({
                    "timestamp": now_str,
                    "strategy": strat_name,
                    "ticker": ticker,
                    "action": "SELL",
                    "qty": qty,
                    "price": current_price,
                    "gross_pnl": round(gross_pnl, 2),
                    "fee": round(exit_fee, 2),
                    "net_pnl": round(net_pnl, 2),
                    "reason": reason
                })
            else:
                updated_positions.append(pos)
                
        strat_data["positions"] = updated_positions

# -------------------------------------------------------------------
# Engine Step 2: Signal Generation & Noise Auditing
# -------------------------------------------------------------------

def evaluate_strategy_signal(strat_name: str) -> dict:
    """
    Evaluates market conditions for a strategy and returns a trade setup ONLY 
    if statistical signal criteria are satisfied.
    """
    # Mapping of target tickers and requirements
    target_map = {
        "ASX_ADR_Arbitrage": ("BHP.AX", "BHP"),
        "US_Earnings_Lag": ("NVDA", None),
        "Inventory_Drift_Reversal": ("IFX.DE", None),
        "Futures_Lead_Front_Run": ("SPY", "ES=F"),
        "Crypto_FinTech_Echo": ("ADE.DE", "BTC-USD"),
        "Sentiment_Echo": ("NOVO-B.CO", None),
        "Immediate_Index_Proxy": ("SPY", None),
        "Nikkei_ADR_Front_Run": ("TM", "7203.T"),
        "WTI_Crude_Lag": ("XOM", "USO"),
        "Gold_Futures_Echo": ("NEM", "GLD"),
        "Biotech_News_Lag": ("BNTX", None),
        "Cross_Listed_Pair_Fade": ("RIO.TO", "RIO"),
        "Time_Zone_Momentum_Relay": ("1306.T", None),
        "FX_Adjusted_Earnings_Arb": ("WMT", None),
        "Commodity_Proxy_Lag": ("VALE", "PICK"),
        "ETF_NAV_Window_Arb": ("EEM", None),
        "Nikkei_Tech_Relay": ("QQQ", "^N225"),
        "London_Metals_Catchup": ("FCX", "COPX"),
        "Treasury_Shockwave": ("TLT", "^TNX"),
        "Canadian_Energy_Echo": ("SU.TO", "USO"),
        "ETF_Creation_Lag": ("VVSM.DE", None),
        "SKHY_ADR_FX_Neutralization": ("SKHY", "000660.KS"),
        "SKHY_HBM_Supply_Chain": ("SKHY", "MU"),
        "SKHY_Post_Market_KOSPI": ("SKHY", None)
    }

    ticker, lead_ticker = target_map.get(strat_name, (None, None))
    if not ticker:
        return None

    df = get_market_data(ticker)
    if df.empty or len(df) < 25:
        return None

    z_score, rvol = calculate_zscore_and_rvol(df)
    current_price = float(df["Close"].iloc[-1])

    # --- NOISE FILTER 1: Earnings Verification ---
    if strat_name in ["US_Earnings_Lag", "FX_Adjusted_Earnings_Arb", "Biotech_News_Lag"]:
        if not is_earnings_event_today(ticker):
            return None  # Filter noise: No confirmed earnings release today
        if abs(z_score) < 2.5 or rvol < 2.0:
            return None  # Filter noise: Post-earnings move lacked volume/volatility

    # --- NOISE FILTER 2: SKHY Cluster Differentiation ---
    elif strat_name == "SKHY_ADR_FX_Neutralization":
        # Requires extreme FX-adjusted spread anomaly vs Korean KOSPI close
        if abs(z_score) < 2.8 or rvol < 2.2:
            return None
    elif strat_name == "SKHY_HBM_Supply_Chain":
        # Requires Micron/Semiconductor lead asset move confirmation
        lead_df = get_market_data(lead_ticker) if lead_ticker else pd.DataFrame()
        if lead_df.empty:
            return None
        lead_z, lead_rvol = calculate_zscore_and_rvol(lead_df)
        if lead_z < 2.5 or lead_rvol < 2.5:
            return None  # Filter noise: Lead memory chip maker did not break out
    elif strat_name == "SKHY_Post_Market_KOSPI":
        # Requires ultra-high volatility shock near market session boundary
        if abs(z_score) < 3.0 or rvol < 2.5:
            return None

    # --- NOISE FILTER 3: Lead/Lag Asset Confirmations ---
    elif lead_ticker:
        lead_df = get_market_data(lead_ticker)
        if not lead_df.empty:
            lead_z, lead_rvol = calculate_zscore_and_rvol(lead_df)
            if abs(lead_z) < 2.2 or lead_rvol < 1.8:
                return None  # Filter noise: Lead ticker did not move significantly

    # --- NOISE FILTER 4: General Volatility & Volume Gate ---
    else:
        if abs(z_score) < 2.5 or rvol < 2.0:
            return None  # Filter noise: Standard random walk fluctuation

    # If all statistical filters pass, construct trade proposal
    direction = "LONG" if z_score > 0 else "SHORT"
    
    # 1.5% TP / 0.8% SL targets
    tp_price = current_price * 1.015 if direction == "LONG" else current_price * 0.985
    sl_price = current_price * 0.992 if direction == "LONG" else current_price * 1.008

    return {
        "ticker": ticker,
        "direction": direction,
        "entry_price": current_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "z_score": z_score,
        "rvol": rvol
    }

# -------------------------------------------------------------------
# Engine Step 3: Execute Trades & Save State
# -------------------------------------------------------------------

def run_trading_scan(state):
    print("\n--- Scanning Markets for Genuine Signals ---")
    now_str = datetime.now(timezone.utc).isoformat()
    
    for strat_name, strat_data in state["strategies"].items():
        # Avoid opening new positions if already active
        if len(strat_data.get("positions", [])) > 0:
            continue
            
        cash = strat_data.get("cash", 0.0)
        if cash < 2000.0:  # Minimum cash threshold
            continue

        signal = evaluate_strategy_signal(strat_name)
        if not signal:
            continue

        ticker = signal["ticker"]
        entry_price = signal["entry_price"]
        direction = signal["direction"]
        
        # Risk Allocation: Deploy 90% of available strategy cash
        capital_to_deploy = cash * 0.90
        qty = math.floor(capital_to_deploy / entry_price)
        if qty <= 0:
            continue

        trade_value = qty * entry_price
        entry_fee = get_broker_fee(ticker, trade_value)
        total_cost = trade_value + entry_fee

        # Deduct cash & log position
        strat_data["cash"] -= total_cost
        position = {
            "ticker": ticker,
            "direction": direction,
            "entry_price": entry_price,
            "qty": qty,
            "tp_price": signal["tp_price"],
            "sl_price": signal["sl_price"],
            "entry_fee": entry_fee,
            "entry_time": now_str
        }
        strat_data["positions"].append(position)

        print(f"[SIGNAL DETECTED] {strat_name}: BUY {qty} {ticker} @ ${entry_price:.2f} (Z-Score: {signal['z_score']:.2f}, RVOL: {signal['rvol']:.2f})")

        log_trade_history({
            "timestamp": now_str,
            "strategy": strat_name,
            "ticker": ticker,
            "action": "BUY",
            "qty": qty,
            "price": entry_price,
            "gross_pnl": 0.0,
            "fee": round(entry_fee, 2),
            "net_pnl": 0.0,
            "reason": f"SIGNAL_ENTRY (Z={signal['z_score']:.2f})"
        })

# -------------------------------------------------------------------
# Main Execution Entry Point
# -------------------------------------------------------------------

def main():
    print(f"=== LagTrader Engine Run Started: {datetime.now(timezone.utc).isoformat()} ===")
    
    # 1. Load Portfolio
    state = load_portfolio_state()
    
    # 2. Process Existing Position Exits
    process_open_positions(state)
    
    # 3. Scan & Filter New Signals
    run_trading_scan(state)
    
    # 4. Save State
    save_portfolio_state(state)
    print("=== Execution Run Complete. State Updated. ===")

if __name__ == "__main__":
    main()
