import os
import json
import csv
from datetime import datetime, timezone
import pandas as pd
import yfinance as yf

# Paths for persisting state inside the GitHub repository
DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "portfolio_state.json")
HISTORY_FILE = os.path.join(DATA_DIR, "trade_history.csv")

# 24 Strategy Definitions ($10,000 virtual capital each)
STRATEGIES_CONFIG = {
    "ASX_ADR_Arbitrage": {"watch": "BHP", "trade": "BHP.AX", "sl_pct": 0.0075, "tp_pct": 0.015, "asset_type": "INTL_EQUITY"},
    "US_Earnings_Lag": {"watch": "NVDA", "trade": "ASML.AS", "sl_pct": 0.015, "tp_pct": 0.045, "asset_type": "INTL_EQUITY"},
    "Inventory_Drift_Reversal": {"watch": "QQQ", "trade": "IFX.DE", "sl_pct": 0.008, "tp_pct": 0.020, "asset_type": "INTL_EQUITY"},
    "Futures_Lead_Front_Run": {"watch": "MNQ=F", "trade": "FDAX.DE", "sl_pct": 0.005, "tp_pct": 0.010, "asset_type": "FUTURES"},
    "Crypto_FinTech_Echo": {"watch": "MSTR", "trade": "ADE.DE", "sl_pct": 0.020, "tp_pct": 0.060, "asset_type": "INTL_EQUITY"},
    "Sentiment_Echo": {"watch": "LLY", "trade": "NOVO-B.CO", "sl_pct": 0.010, "tp_pct": 0.015, "asset_type": "INTL_EQUITY"},
    "Immediate_Index_Proxy": {"watch": "SPY", "trade": "CSPX.L", "sl_pct": 0.005, "tp_pct": 0.005, "asset_type": "INTL_EQUITY"},
    "Nikkei_ADR_Front_Run": {"watch": "TSLA", "trade": "7203.T", "sl_pct": 0.010, "tp_pct": 0.020, "asset_type": "INTL_EQUITY"},
    "WTI_Crude_Lag": {"watch": "CL=F", "trade": "BP.L", "sl_pct": 0.012, "tp_pct": 0.030, "asset_type": "INTL_EQUITY"},
    "Gold_Futures_Echo": {"watch": "GC=F", "trade": "GOL.PA", "sl_pct": 0.008, "tp_pct": 0.016, "asset_type": "INTL_EQUITY"},
    "Biotech_News_Lag": {"watch": "MRNA", "trade": "BNTX", "sl_pct": 0.015, "tp_pct": 0.045, "asset_type": "US_EQUITY"},
    "Cross_Listed_Pair_Fade": {"watch": "RIO", "trade": "RIO.TO", "sl_pct": 0.006, "tp_pct": 0.012, "asset_type": "INTL_EQUITY"},
    "Time_Zone_Momentum_Relay": {"watch": "SPY", "trade": "1306.T", "sl_pct": 0.007, "tp_pct": 0.014, "asset_type": "INTL_EQUITY"},
    "FX_Adjusted_Earnings_Arb": {"watch": "TGT", "trade": "WMT", "sl_pct": 0.010, "tp_pct": 0.025, "asset_type": "US_EQUITY"},
    "Commodity_Proxy_Lag": {"watch": "CL=F", "trade": "SU.TO", "sl_pct": 0.012, "tp_pct": 0.036, "asset_type": "INTL_EQUITY"},
    "ETF_NAV_Window_Arb": {"watch": "SPY", "trade": "CSPX.L", "sl_pct": 0.004, "tp_pct": 0.006, "asset_type": "INTL_EQUITY"},
    "Nikkei_Tech_Relay": {"watch": "9984.T", "trade": "QQQ", "sl_pct": 0.006, "tp_pct": 0.012, "asset_type": "US_EQUITY"},
    "London_Metals_Catchup": {"watch": "RIO.L", "trade": "FCX", "sl_pct": 0.007, "tp_pct": 0.014, "asset_type": "US_EQUITY"},
    "Treasury_Shockwave": {"watch": "ZN=F", "trade": "NK225M.OS", "sl_pct": 0.008, "tp_pct": 0.020, "asset_type": "FUTURES"},
    "Canadian_Energy_Echo": {"watch": "XOM", "trade": "SU.TO", "sl_pct": 0.009, "tp_pct": 0.018, "asset_type": "INTL_EQUITY"},
    "ETF_Creation_Lag": {"watch": "SMH", "trade": "VVSM.DE", "sl_pct": 0.005, "tp_pct": 0.0075, "asset_type": "INTL_EQUITY"},
    "SKHY_ADR_FX_Neutralization": {"watch": "000660.KS", "trade": "SKHY", "sl_pct": 0.010, "tp_pct": 0.025, "asset_type": "US_EQUITY"},
    "SKHY_HBM_Supply_Chain": {"watch": "MU", "trade": "SKHY", "sl_pct": 0.012, "tp_pct": 0.030, "asset_type": "US_EQUITY"},
    "SKHY_Post_Market_KOSPI": {"watch": "NVDA", "trade": "SKHY", "sl_pct": 0.015, "tp_pct": 0.035, "asset_type": "US_EQUITY"}
}

def calculate_ibkr_fee(asset_type: str, qty: float, price: float) -> float:
    """Calculates estimated IBKR Pro commission fees per order."""
    trade_value = qty * price
    if asset_type == "US_EQUITY":
        # Tiered: $0.0035 per share (Min $0.35, Max 1% of trade value)
        fee = max(0.35, qty * 0.0035)
        return min(fee, trade_value * 0.01)
    elif asset_type == "FUTURES":
        # Fixed contract fee estimate (~$0.85 / contract)
        return max(0.85, qty * 0.85)
    else:  # INTL_EQUITY
        # Approx 0.05% of trade value with $2.50 minimum
        return max(2.50, trade_value * 0.0005)

def initialize_or_load_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(STATE_FILE):
        state = {"total_capital": 240000.0, "strategies": {}}
        for strat_name in STRATEGIES_CONFIG:
            state["strategies"][strat_name] = {
                "allocated": 10000.0,
                "cash": 10000.0,
                "positions": []
            }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        return state
    else:
        with open(STATE_FILE, "r") as f:
            return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def log_trade(strat_name, ticker, direction, entry_p, exit_p, qty, pnl, fee, reason):
    file_exists = os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "strategy", "ticker", "direction", "qty", "entry_price", "exit_price", "gross_pnl", "ibkr_fee", "net_pnl", "exit_reason"])
        net_pnl = pnl - fee
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), strat_name, ticker, direction,
            qty, round(entry_p, 4), round(exit_p, 4), round(pnl, 2), round(fee, 2), round(net_pnl, 2), reason
        ])

def fetch_latest_price(ticker):
    try:
        data = yf.download(ticker, period="1d", interval="1m", progress=False)
        if not data.empty:
            close_val = data["Close"].iloc[-1]
            return float(close_val.iloc[0]) if isinstance(close_val, pd.Series) else float(close_val)
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
    return None

def process_engine():
    state = initialize_or_load_state()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now_str}] Starting NostraTrade 24-Strategy Execution...")

    # Phase 1: Process Open Positions for TP / SL Exits
    for strat_name, strat_data in state["strategies"].items():
        cfg = STRATEGIES_CONFIG[strat_name]
        active_positions = strat_data["positions"]
        remaining_positions = []

        for pos in active_positions:
            ticker = pos["ticker"]
            current_price = fetch_latest_price(ticker)
            if not current_price:
                remaining_positions.append(pos)
                continue

            entry_price = pos["entry_price"]
            qty = pos["qty"]
            tp_price = pos["tp_price"]
            sl_price = pos["sl_price"]
            direction = pos["direction"]

            # Exit Conditions
            hit_tp = (direction == "LONG" and current_price >= tp_price) or (direction == "SHORT" and current_price <= tp_price)
            hit_sl = (direction == "LONG" and current_price <= sl_price) or (direction == "SHORT" and current_price >= sl_price)

            if hit_tp or hit_sl:
                reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
                gross_pnl = (current_price - entry_price) * qty if direction == "LONG" else (entry_price - current_price) * qty
                exit_fee = calculate_ibkr_fee(cfg["asset_type"], qty, current_price)
                total_fees = pos["entry_fee"] + exit_fee
                net_pnl = gross_pnl - total_fees

                strat_data["cash"] += (qty * current_price) + net_pnl
                log_trade(strat_name, ticker, direction, entry_price, current_price, qty, gross_pnl, total_fees, reason)
                print(f"[{strat_name}] CLOSED {direction} on {ticker} via {reason}. Net P&L: ${net_pnl:.2f}")
            else:
                remaining_positions.append(pos)

        strat_data["positions"] = remaining_positions

    # Phase 2: Check Signals & Trigger New Trades
    for strat_name, strat_data in state["strategies"].items():
        # Maximum 1 active position per strategy at a time
        if len(strat_data["positions"]) > 0:
            continue

        cfg = STRATEGIES_CONFIG[strat_name]
        watch_price = fetch_latest_price(cfg["watch"])
        trade_price = fetch_latest_price(cfg["trade"])

        if not watch_price or not trade_price:
            continue

        # Strategy Signal Trigger (Standardized 0.5% momentum delta check)
        signal = "LONG" if watch_price > trade_price * 1.005 else None

        if signal:
            cash = strat_data["cash"]
            if cash < 500: # Minimum capital safety buffer
                continue

            trade_amount = cash * 0.95 # Deploy 95% of available strategy cash
            qty = int(trade_amount / trade_price) if cfg["asset_type"] != "FUTURES" else max(1, int(trade_amount / (trade_price * 0.10)))

            if qty <= 0:
                continue

            entry_fee = calculate_ibkr_fee(cfg["asset_type"], qty, trade_price)
            tp_price = trade_price * (1 + cfg["tp_pct"]) if signal == "LONG" else trade_price * (1 - cfg["tp_pct"])
            sl_price = trade_price * (1 - cfg["sl_pct"]) if signal == "LONG" else trade_price * (1 + cfg["sl_pct"])

            position = {
                "ticker": cfg["trade"],
                "direction": signal,
                "entry_price": trade_price,
                "qty": qty,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "entry_fee": entry_fee,
                "entry_time": datetime.now(timezone.utc).isoformat()
            }

            strat_data["cash"] -= (qty * trade_price) + entry_fee
            strat_data["positions"].append(position)
            print(f"[{strat_name}] OPENED {signal} on {cfg['trade']} at ${trade_price:.2f}. Qty: {qty}, TP: ${tp_price:.2f}, SL: ${sl_price:.2f}")

    save_state(state)
    print(f"[{now_str}] Engine Run Complete. Portfolio state persisted.")

if __name__ == "__main__":
    process_engine()
