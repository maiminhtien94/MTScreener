
import ccxt
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt
import time

# Initialize Binance Futures connection with Rate Limiter
exchange = ccxt.binance({
    'rateLimit': True, 
    'options': {'defaultType': 'future'}
})

def get_active_trading_symbols():
    try:
        markets = exchange.load_markets()
        tickers = exchange.fetch_tickers()
        active_symbols = []
        for s, info in markets.items():
            if '/USDT:USDT' in s:
                is_active = info.get('active', False)
                market_info = info.get('info', {})
                status = market_info.get('status', market_info.get('contractStatus', '')).upper()
                
                ticker_info = tickers.get(s, {})
                v24h = ticker_info.get('baseVolume', 0) if ticker_info else 0
                
                if is_active and status == 'TRADING' and v24h > 0:
                    active_symbols.append(s)
        return active_symbols
    except Exception as e:
        print(f"Error fetching active trading symbols: {e}")
        return []

def get_data(symbol, timeframe):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=250)
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # Calculate EMA 34 and EMA 89 using native Pandas
        df['ema34'] = df['close'].ewm(span=34, adjust=False).mean()
        df['ema89'] = df['close'].ewm(span=89, adjust=False).mean()
        return df
    except Exception:
        return pd.DataFrame()
def check_direction_flexible(df, lookback_bars=12):
    """
    Checks if at least 90% of recent candle bodies (excluding wicks) 
    remain strictly above or below both EMAs for the lookback period.
    """
    if df.empty or len(df) < (lookback_bars + 89):
        return None  
    
    # Extract the recent window
    recent_bars = df.iloc[-lookback_bars:]
    
    # Filter out low liquidity assets
    if recent_bars['volume'].sum() == 0 or recent_bars['volume'].iloc[-1] == 0:
        return None
    
    latest_bar = df.iloc[-1]
    
    # Calculate the highest and lowest parts of the candle body (Native Pandas)
    candle_body_bottom = recent_bars[['open', 'close']].min(axis=1)
    candle_body_top = recent_bars[['open', 'close']].max(axis=1)
    
    # 1. BULLISH ALIGNMENT (Targeting ~90% of bodies ABOVE EMAs)
    ema_order_up = latest_bar['ema34'] > latest_bar['ema89']
    
    # Sum the boolean values (True = 1, False = 0) to get the count of valid candles
    above_34_count = (candle_body_bottom > recent_bars['ema34']).sum()
    above_89_count = (candle_body_bottom > recent_bars['ema89']).sum()
    
    # Calculate percentages
    pct_above_34 = above_34_count / lookback_bars
    pct_above_89 = above_89_count / lookback_bars
    
    # Trigger UP if EMA structure is correct AND at least 90% of bars are above both EMAs
    if ema_order_up and pct_above_34 >= 0.90 and pct_above_89 >= 0.90:
        return "UP"
        
    # 2. BEARISH ALIGNMENT (Targeting ~90% of bodies BELOW EMAs)
    ema_order_down = latest_bar['ema34'] < latest_bar['ema89']
    
    below_34_count = (candle_body_top < recent_bars['ema34']).sum()
    below_89_count = (candle_body_top < recent_bars['ema89']).sum()
    
    # Calculate percentages
    pct_below_34 = below_34_count / lookback_bars
    pct_below_89 = below_89_count / lookback_bars
    
    # Trigger DOWN if EMA structure is correct AND at least 90% of bars are below both EMAs
    if ema_order_down and pct_below_34 >= 0.90 and pct_below_89 >= 0.80:
        return "DOWN"
        
    return None

def plot_crypto_chart(symbol_name, limit_bars=100):
    """
    Plots H1 and H4 candlestick charts side-by-side in a single window.
    Fixed the subplots bug by using native matplotlib grids.
    """
    if '/' not in symbol_name:
        base = symbol_name.replace('USDT', '')
        symbol = f"{base}/USDT:USDT"
    else:
        symbol = symbol_name

    print(f"\n[INFO] Opening side-by-side (H1 | H4) chart for {symbol}...")

    try:
        # 1. Fetch data for both timeframes
        df_h1 = get_data(symbol, '1h')
        df_h4 = get_data(symbol, '4h')
        
        if df_h1.empty or df_h4.empty:
            print("[ERROR] Failed to fetch data for H1 or H4.")
            return

        # 2. Process DatetimeIndex for H1
        df_h1['timestamp'] = pd.to_datetime(df_h1['timestamp'], unit='ms')
        df_h1.set_index('timestamp', inplace=True)
        plot_h1 = df_h1.iloc[-limit_bars:]

        # 3. Process DatetimeIndex for H4
        df_h4['timestamp'] = pd.to_datetime(df_h4['timestamp'], unit='ms')
        df_h4.set_index('timestamp', inplace=True)
        plot_h4 = df_h4.iloc[-limit_bars:]

        # 4. Define TradingView Dark Style
        custom_style = mpf.make_mpf_style(
            base_mpf_style='charles',
            marketcolors=mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit', volume='inherit'),
            figcolor='#131722', facecolor='#1c2030', gridcolor='#2a2e39', gridstyle=':'
        )

        # 5. Create layout using native Matplotlib subplots
        # 2 columns (Left: H1, Right: H4), each column has 2 rows (Top: Candle, Bottom: Volume)
        # gridspec_kw sets the height ratio between Candle (3) and Volume (1)
        fig, axes = plt.subplots(
            nrows=2, ncols=2, figsize=(16, 9), sharex='col',
            gridspec_kw={'height_ratios': [3, 1]}
        )
        
        # Map axes to specific charts
        ax_h1_candle = axes[0, 0]
        ax_h1_vol    = axes[1, 0]
        ax_h4_candle = axes[0, 1]
        ax_h4_vol    = axes[1, 1]

        # 6. Configure EMA indicators and link them to their respective candle axes
        indicators_h1 = [
            mpf.make_addplot(plot_h1['ema34'], color='#ff9800', width=1.2, ax=ax_h1_candle),
            mpf.make_addplot(plot_h1['ema89'], color='#4caf50', width=1.2, ax=ax_h1_candle)
        ]
        indicators_h4 = [
            mpf.make_addplot(plot_h4['ema34'], color='#ff9800', width=1.2, ax=ax_h4_candle),
            mpf.make_addplot(plot_h4['ema89'], color='#4caf50', width=1.2, ax=ax_h4_candle)
        ]

        # 7. Plot H1 Chart (Left column)
        mpf.plot(
            plot_h1, type='candle', ax=ax_h1_candle, volume=ax_h1_vol, 
            addplot=indicators_h1, style=custom_style
        )
        ax_h1_candle.set_title(f"{symbol} - 1H TIMEFRAME", color='white', fontsize=12, fontweight='bold')
        ax_h1_candle.set_ylabel('Price (USDT)', color='white')
        ax_h1_vol.set_ylabel('Volume', color='white')

        # 8. Plot H4 Chart (Right column)
        mpf.plot(
            plot_h4, type='candle', ax=ax_h4_candle, volume=ax_h4_vol, 
            addplot=indicators_h4, style=custom_style
        )
        ax_h4_candle.set_title(f"{symbol} - 4H TIMEFRAME", color='white', fontsize=12, fontweight='bold')
        ax_h4_candle.set_ylabel('Price (USDT)', color='white')
        ax_h4_vol.set_ylabel('Volume', color='white')

        # 9. Format figure appearance
        fig.suptitle(f"Multi-Timeframe Analysis: {symbol}", color='white', fontsize=16, fontweight='bold', y=0.98)
        fig.patch.set_facecolor('#131722') # Set window outer background color
        
        plt.tight_layout()
        plt.show() # Display the layout

    except Exception as e:
        print(f"[ERROR] Dual-chart plotting failed: {e}")

def scan_market():
    print("Scanning the active Futures market... Please wait.\n")
    symbols = get_active_trading_symbols()
    if not symbols:
        print("No active trading pairs found. Terminating scan.")
        return
        
    print(f"Found {len(symbols)} active USDT pairs. Starting trend scan...\n")
    
    list_up = []
    list_down = []
    LOOKBACK_PERIOD = 20

    for symbol in symbols:
        try:
            df_h1 = get_data(symbol, '1h')
            dir_h1 = check_direction_flexible(df_h1, lookback_bars=LOOKBACK_PERIOD)
            
            if not dir_h1:
                time.sleep(0.02)
                continue
                
            df_h4 = get_data(symbol, '4h')
            LOOKBACK_PERIOD = 40
            dir_h4 = check_direction_flexible(df_h4, lookback_bars=LOOKBACK_PERIOD)
            
            clean_symbol = symbol.split(':')[0].replace('/', '')
            
            if dir_h1 == "UP" and dir_h4 == "UP":
                list_up.append(clean_symbol)
                print(f"🔥 LONG Signal Detected: {clean_symbol}")
                break
            elif dir_h1 == "DOWN" and dir_h4 == "DOWN":
                list_down.append(clean_symbol)
                print(f"❄️ SHORT Signal Detected: {clean_symbol}")
                break
                
            time.sleep(0.02) 
        except Exception:
            continue

    # Combine both lists into a unified valid target list for user selection
    all_signals = list_up + list_down

    print("\n======================= SCANNER RESULTS =======================")
    print(f"\n📈 LONG SIGNALS (Last {LOOKBACK_PERIOD} Bars):")
    if list_up:
        for idx, coin in enumerate(list_up, 1):
            print(f"  [{idx}] {coin}")
    else:
        print("  (No assets found)")
        
    print(f"\n📉 SHORT SIGNALS (Last {LOOKBACK_PERIOD} Bars):")
    if list_down:
        # Continue index numbers from the last list
        for idx, coin in enumerate(list_down, len(list_up) + 1):
            print(f"  [{idx}] {coin}")
    else:
        print("  (No assets found)")
    print("\n===========================================================")

    # INTERACTIVE CLICK/SELECTION MENU
    if not all_signals:
        print("\nNo signals detected. Exiting system.")
        return

    while True:
        print(f"\n[MENU] Enter a number [1-{len(all_signals)}] or type the COIN NAME (e.g., 'SPORTFUNUSDT') to view H1 chart.")
        print("Type 'exit' to quit the application.")
        user_input = input("Your selection: ").strip().upper()

        if user_input == 'EXIT':
            print("System closed. Goodbye!")
            break

        selected_coin = None

        # Check if the user entered a valid number
        if user_input.isdigit():
            choice_num = int(user_input)
            if 1 <= choice_num <= len(all_signals):
                selected_coin = all_signals[choice_num - 1]
            else:
                print(f"[WARN] Invalid number. Please choose between 1 and {len(all_signals)}.")
                continue
        else:
            # Check if user typed the literal coin name (e.g., BTCUSDT)
            if user_input in all_signals:
                selected_coin = user_input
            else:
                print("[WARN] Coin not found in the current signal list. Try again.")
                continue

        # If a valid coin is determined, call the plotting engine
        if selected_coin:
            plot_crypto_chart(symbol_name=selected_coin,limit_bars=100)

if __name__ == "__main__":
    scan_market()
    #plot_crypto_chart("ETHUSDT",limit_bars=80)


