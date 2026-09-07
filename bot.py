import yfinance as yf
import pandas as pd
from datetime import datetime

def run_strategy():
    ticker = "AAPL"
    print(f"[{datetime.now()}] Running LagTrader strategy for {ticker}...")

    # Fetch latest price data
    data = yf.download(ticker, period="5d", interval="15m")
    if data.empty:
        print("No market data fetched.")
        return

    # Simple Moving Average calculation
    data['SMA_20'] = data['Close'].rolling(window=20).mean()
    data['SMA_50'] = data['Close'].rolling(window=50).mean()

    latest_close = float(data['Close'].iloc[-1].iloc[0]) if isinstance(data['Close'].iloc[-1], pd.Series) else float(data['Close'].iloc[-1])
    sma_20 = float(data['SMA_20'].iloc[-1].iloc[0]) if isinstance(data['SMA_20'].iloc[-1], pd.Series) else float(data['SMA_20'].iloc[-1])
    sma_50 = float(data['SMA_50'].iloc[-1].iloc[0]) if isinstance(data['SMA_50'].iloc[-1], pd.Series) else float(data['SMA_50'].iloc[-1])

    print(f"Current Price: ${latest_close:.2f}")
    print(f"SMA 20: ${sma_20:.2f} | SMA 50: ${sma_50:.2f}")

    # Generate simulated trade signal
    if sma_20 > sma_50:
        print("SIGNAL: BULLISH BUY (SMA 20 above SMA 50)")
    else:
        print("SIGNAL: NEUTRAL / BEARISH")

if __name__ == "__main__":
    run_strategy()
