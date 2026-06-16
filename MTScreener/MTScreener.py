import ccxt
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt
import time
import matplotlib.animation as animation
import warnings
import re  # regex
from concurrent.futures import ThreadPoolExecutor, as_completed

# 🎯 Dập tắt hoàn toàn các dòng cảnh báo rác về thiếu font chữ hệ thống
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")
plt.rcParams['axes.unicode_minus'] = False 

# Initialize Binance Futures connection with Rate Limiter
exchange = ccxt.binance({
    'rateLimit': True, 
    'options': {'defaultType': 'future'}
})

# --- BIẾN TOÀN CỤC PHỤC VỤ ĐIỀU HƯỚNG TƯƠNG TÁC CHỐNG LAG ---
list_signals = []         # Danh sách coin quét được để chuyển đổi
current_index = 0         # Vị trí coin hiện tại trong danh sách
ani = None                # Giữ luồng tự động cập nhật đồ thị
current_tf_mode = "H4_H1" # Chế độ khung thời gian mặc định ("H4_H1" hoặc "H1_M5")
crosshairs = {}           # Lưu trữ các đường dóng chữ thập cho các trục
time_cache = {}           # Bộ nhớ đệm lưu DatetimeIndex gốc của từng trục
bm_background = None      # KỸ THUẬT BLITTING: Ảnh chụp bộ nhớ đệm đồ thị nền cố định
is_updating = False       # KHÓA LUỒNG: Chống xung đột dữ liệu ngầm

# --- BIẾN TOÀN CỤC CHO CÔNG CỤ THƯỚC ĐO % ---
ruler_start = None        # Lưu tọa độ điểm click đầu tiên (x, y)
ruler_elements = {}       # Lưu trữ các đối tượng vẽ thước đo (hộp màu và text) trên các trục
current_custom_symbol = None  # 🎯 QUAN TRỌNG: Lưu trữ mã tùy chỉnh ngoài danh sách nếu có

def get_active_trading_symbols():
    """
    Phiên bản lọc tối ưu:
    - Loại bỏ ngay lập tức các coin có chứa chữ Trung Quốc (币安...) hoặc ký tự lạ.
    - Chỉ lấy hợp đồng VĨNH CỬU (Perpetual).
    """
    try:
        # Gọi thẳng tickers của các cặp Futures từ Binance
        tickers = exchange.fetch_tickers()
        active_symbols = []
        
        for symbol, ticker_info in tickers.items():
            # 1. 🎯 BỘ LỌC TÊN COIN: Loại bỏ chữ Trung Quốc / Ký tự lạ ngay từ đầu
            # Trích xuất phần tên coin thô trước dấu gạch chéo (Ví dụ: 'BTC/USDT:USDT' -> 'BTC')
            base_coin = symbol.split('/')[0].upper()
            
            # Nếu tên coin chứa bất kỳ ký tự nào KHÔNG PHẢI chữ tiếng Anh (A-Z) hoặc số (0-9)
            # Hệ thống sẽ bỏ qua ngay (Mấy con dạng '币安人生' sẽ bị chặn đứng tại đây)
            if not re.match(r'^[A-Z0-9]+$', base_coin):
                continue
                
            # 2. 🎯 CHỈ LẤY HỢP ĐỒNG VĨNH CỬU (Perpetual)
            # Tránh trùng lặp mã với các hợp đồng giao ngay theo Quý
            if symbol.endswith('/USDT:USDT'):
                
                # 3. Kiểm tra trạng thái hoạt động và thanh khoản của sàn
                raw_info = ticker_info.get('info', {})
                status = raw_info.get('status', '').upper()
                v24h = ticker_info.get('baseVolume', 0)
                current_price = ticker_info.get('last', 0)
                
                if (status == 'TRADING' or current_price > 0) and v24h > 0:
                    active_symbols.append(symbol)
                    
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
        df['ema34'] = df['close'].ewm(span=34, adjust=False).mean()
        df['ema89'] = df['close'].ewm(span=89, adjust=False).mean()
        return df
    except Exception:
        return pd.DataFrame()

def check_direction_flexible(df, lookback_bars=12):
    if df.empty or len(df) < (lookback_bars + 89):
        return None  
    recent_bars = df.iloc[-lookback_bars:]
    if recent_bars['volume'].sum() == 0 or recent_bars['volume'].iloc[-1] == 0:
        return None
    latest_bar = df.iloc[-1]
    candle_body_bottom = recent_bars[['open', 'close']].min(axis=1)
    candle_body_top = recent_bars[['open', 'close']].max(axis=1)
    
    # 1. BULLISH ALIGNMENT
    ema_order_up = latest_bar['ema34'] > latest_bar['ema89']
    if ema_order_up and (candle_body_bottom > recent_bars['ema34']).sum() / lookback_bars >= 0.90 and (candle_body_bottom > recent_bars['ema89']).sum() / lookback_bars >= 0.90:
        return "UP"
    # 2. BEARISH ALIGNMENT
    ema_order_down = latest_bar['ema34'] < latest_bar['ema89']
    if ema_order_down and (candle_body_top < recent_bars['ema34']).sum() / lookback_bars >= 0.90 and (candle_body_top < recent_bars['ema89']).sum() / lookback_bars >= 0.80:
        return "DOWN"
    return None

def check_ltf_setup(df_ltf, htf_direction, max_distance_pct=1.5):
    if df_ltf.empty or len(df_ltf) < 2:
        return False
    latest_candle = df_ltf.iloc[-1]
    close_price = latest_candle['close']
    ema34 = latest_candle['ema34']
    ema89 = latest_candle['ema89']
    
    if htf_direction == "UP" and ema34 > ema89 and close_price > ema34:
        if ((close_price - ema34) / ema34) * 100 <= max_distance_pct: return True
    elif htf_direction == "DOWN" and ema34 < ema89 and close_price < ema34:
        if ((ema34 - close_price) / ema34) * 100 <= max_distance_pct: return True
    return False

# --- ENGINE MOUSE VÀ ĐIỀU HƯỚNG TƯƠNG TÁC TỐI ƯU ---

def on_mouse_press(event):
    global ruler_start, is_updating
    if is_updating or event.button != 1 or not event.inaxes:
        return
    ruler_start = (event.xdata, event.ydata)

def on_mouse_release(event):
    global ruler_start, ruler_elements, bm_background
    if event.button != 1:
        return
    ruler_start = None
    fig = event.canvas.figure
    for ax, elements in ruler_elements.items():
        elements['rect'].set_visible(False)
        elements['text'].set_visible(False)
    if bm_background and fig:
        fig.canvas.restore_region(bm_background)
        fig.canvas.blit(fig.bbox)

def on_mouse_move(event):
    global time_cache, bm_background, crosshairs, is_updating, ruler_start, ruler_elements
    
    if is_updating or bm_background is None or not event.inaxes or event.inaxes not in time_cache:
        return
    
    current_ax = event.inaxes
    x_target, y_target = event.xdata, event.ydata
    cross_color = '#5d606b'
    fig = event.canvas.figure
    
    if current_ax not in crosshairs:
        crosshairs[current_ax] = {
            'v_line': current_ax.axvline(x_target, color=cross_color, linestyle='--', linewidth=0.8),
            'h_line': current_ax.axhline(y_target, color=cross_color, linestyle='--', linewidth=0.8),
            'text': current_ax.text(0, 0, "", color='white', fontsize=7.5,
                                    bbox=dict(facecolor='#2a2e39', alpha=0.85, boxstyle='round,pad=0.2'),
                                    visible=False)
        }
        
    if current_ax not in ruler_elements:
        ruler_elements[current_ax] = {
            'rect': plt.Rectangle((0, 0), 0, 0, facecolor='#26a69a', alpha=0.2, visible=False),
            'text': current_ax.text(0, 0, "", color='white', fontsize=8, fontweight='bold',
                                    bbox=dict(facecolor='#1e222d', alpha=0.9, edgecolor='#26a69a', boxstyle='round,pad=0.3'),
                                    visible=False)
        }
        current_ax.add_patch(ruler_elements[current_ax]['rect'])

    # Blitting khôi phục nền cũ từ RAM
    fig.canvas.restore_region(bm_background)
    
    lines = crosshairs[current_ax]
    lines['v_line'].set_xdata([x_target, x_target])
    lines['h_line'].set_ydata([y_target, y_target])
    lines['v_line'].set_visible(True)
    lines['h_line'].set_visible(True)
    
    # Thuật toán kéo thước đo %
    r_elem = ruler_elements[current_ax]
    if ruler_start is not None:
        x_start, y_start = ruler_start
        width = x_target - x_start
        height = y_target - y_start
        
        r_elem['rect'].set_xy((x_start, y_start))
        r_elem['rect'].set_width(width)
        r_elem['rect'].set_height(height)
        
        if height >= 0:
            r_elem['rect'].set_facecolor('#26a69a') 
            r_elem['text'].get_bbox_patch().set_edgecolor('#26a69a')
        else:
            r_elem['rect'].set_facecolor('#ef5350') 
            r_elem['text'].get_bbox_patch().set_edgecolor('#ef5350')
        r_elem['rect'].set_visible(True)
        
        bars_cnt = abs(int(round(x_target)) - int(round(x_start)))
        pct_change = (height / y_start) * 100
        r_elem['text'].set_text(f"  Biến động: {pct_change:+.2f}%  \n  Số nến: {bars_cnt} bars  ")
        r_elem['text'].set_position((x_target, y_target))
        r_elem['text'].set_visible(True)
    else:
        r_elem['rect'].set_visible(False)
        r_elem['text'].set_visible(False)

    # Đọc thời gian chuẩn xác từ DatetimeIndex gốc
    try:
        idx = int(round(x_target))
        ax_datetime_index = time_cache[current_ax]
        if 0 <= idx < len(ax_datetime_index):
            time_str = ax_datetime_index[idx].strftime('%b %d, %H:%M')
            if ruler_start is not None:
                lines['text'].set_visible(False) 
            else:
                lines['text'].set_text(f" P: {y_target:.4f} \n T: {time_str} ")
                lines['text'].set_position((x_target, y_target))
                lines['text'].set_visible(True)
        else: lines['text'].set_visible(False)
    except Exception:
        lines['text'].set_visible(False)

    # Ẩn các trục không tương tác
    for ax, ax_lines in crosshairs.items():
        if ax != current_ax:
            ax_lines['v_line'].set_visible(False)
            ax_lines['h_line'].set_visible(False)
            ax_lines['text'].set_visible(False)
    for ax, r_el in ruler_elements.items():
        if ax != current_ax:
            r_el['rect'].set_visible(False)
            r_el['text'].set_visible(False)

    # Đẩy pixel đồ họa động lên màn hình
    for ax, ax_lines in crosshairs.items():
        if ax_lines['v_line'].get_visible():
            ax.draw_artist(ax_lines['v_line'])
            ax.draw_artist(ax_lines['h_line'])
            if ax_lines['text'].get_visible():
                ax.draw_artist(ax_lines['text'])
                
    for ax, r_el in ruler_elements.items():
        if r_el['rect'].get_visible() and r_el['rect'].figure is not None:
            ax.draw_artist(r_el['rect'])
        if r_el['text'].get_visible() and r_el['text'].figure is not None:
            ax.draw_artist(r_el['text'])
                
    fig.canvas.blit(fig.bbox)

def render_charts(fig, axes, symbol, limit_bars, custom_style):
    global current_tf_mode, is_updating, bm_background, crosshairs, time_cache, ruler_start, list_signals, current_index
    
    # Khóa luồng an toàn khi người dùng đang đo thước
    if ruler_start is not None:
        return
        
    is_updating = True
    if '/' not in symbol:
        base = symbol.replace('USDT', '')
        ccxt_symbol = f"{base}/USDT:USDT"
    else:
        ccxt_symbol = symbol
    
    if current_tf_mode == "H4_H1":
        tf_left, tf_right = '4h', '1h'
    else:
        tf_left, tf_right = '1h', '5m'
        
    print(f"[RE-RENDER] Loading {symbol} with Mode: {current_tf_mode} ({tf_left.upper()} | {tf_right.upper()})...")
    
    df_left = get_data(ccxt_symbol, tf_left)
    df_right = get_data(ccxt_symbol, tf_right)
    
    if df_left.empty or df_right.empty:
        print(f"[ERROR] Failed to fetch data for {symbol}")
        fig.suptitle(f"⚠️ {symbol} - Tải dữ liệu thất bại!", color='red', fontsize=14, fontweight='bold', y=0.98)
        fig.canvas.draw_idle()
        is_updating = False
        return

    # Giải phóng đối tượng cũ trên các trục
    for ax in axes.flatten():
        while ax.lines: ax.lines[0].remove()
        while ax.collections: ax.collections[0].remove()
        while ax.patches: ax.patches[0].remove()
        while ax.texts: ax.texts[0].remove()

    # Đồng bộ hóa dữ liệu thời gian dạng Datetime
    df_left['timestamp'] = pd.to_datetime(df_left['timestamp'], unit='ms')
    df_left.set_index('timestamp', inplace=True)
    plot_left = df_left.iloc[-limit_bars:]

    df_right['timestamp'] = pd.to_datetime(df_right['timestamp'], unit='ms')
    df_right.set_index('timestamp', inplace=True)
    plot_right = df_right.iloc[-limit_bars:]

    ax_left_candle, ax_left_vol = axes[0, 0], axes[1, 0]
    ax_right_candle, ax_right_vol = axes[0, 1], axes[1, 1]

    indicators_left = [
        mpf.make_addplot(plot_left['ema34'], color='#ff9800', width=1.2, ax=ax_left_candle),
        mpf.make_addplot(plot_left['ema89'], color='#4caf50', width=2.4, ax=ax_left_candle)
    ]
    indicators_right = [
        mpf.make_addplot(plot_right['ema34'], color='#ff9800', width=1.2, ax=ax_right_candle),
        mpf.make_addplot(plot_right['ema89'], color='#4caf50', width=2.4, ax=ax_right_candle)
    ]

    mpf.plot(plot_left, type='candle', ax=ax_left_candle, volume=ax_left_vol, addplot=indicators_left, style=custom_style)
    mpf.plot(plot_right, type='candle', ax=ax_right_candle, volume=ax_right_vol, addplot=indicators_right, style=custom_style)

    ax_left_candle.set_title(f"{symbol} - {tf_left.upper()}", color='white', fontsize=11, fontweight='bold')
    ax_right_candle.set_title(f"{symbol} - {tf_right.upper()}", color='white', fontsize=11, fontweight='bold')
    ax_left_candle.set_ylabel('Price (USDT)', color='white')
    ax_left_vol.set_ylabel('Volume', color='white')

    text_color, border_color, FONT_SIZE = '#b2b5be', '#2a2e39', 7.5

    for ax in axes.flatten():
        ax.tick_params(axis='both', which='both', colors=text_color, labelsize=FONT_SIZE)
        ax.yaxis.label.set_color(text_color)
        ax.xaxis.label.set_color(text_color)
        for spine in ax.spines.values():
            spine.set_edgecolor(border_color)

    ax_left_candle.tick_params(labelbottom=True)
    ax_right_candle.tick_params(labelbottom=True)
    plt.setp(ax_left_candle.get_xticklabels(), rotation=30, horizontalalignment='right', fontsize=FONT_SIZE)
    plt.setp(ax_right_candle.get_xticklabels(), rotation=30, horizontalalignment='right', fontsize=FONT_SIZE)
    plt.setp(ax_left_vol.get_xticklabels(), rotation=30, horizontalalignment='right', fontsize=FONT_SIZE)
    plt.setp(ax_right_vol.get_xticklabels(), rotation=30, horizontalalignment='right', fontsize=FONT_SIZE)

    for ax in [ax_left_candle, ax_left_vol, ax_right_candle, ax_right_vol]:
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")

    # Lưu mảng DatetimeIndex thô của nến vào cache
    time_cache.clear()
    time_cache[ax_left_candle] = plot_left.index
    time_cache[ax_left_vol]    = plot_left.index
    time_cache[ax_right_candle] = plot_right.index
    time_cache[ax_right_vol]    = plot_right.index

    # Tính toán hiển thị số thứ tự cho chuẩn xác
    info_idx = f"[{current_index + 1}/{len(list_signals)}]" if list_signals and symbol in list_signals else "[CUSTOM COIN]"
    fig.suptitle(f"Multi-Timeframe ({tf_left.upper()} | {tf_right.upper()}): {symbol}  {info_idx}", 
                 color='white', fontsize=14, fontweight='bold', y=0.98)
    fig.patch.set_facecolor('#131722')
    
    fig.canvas.draw()
    crosshairs.clear() 
    global ruler_elements
    ruler_elements.clear() 
    
    bm_background = fig.canvas.copy_from_bbox(fig.bbox) 
    fig.canvas.draw_idle()
    
    is_updating = False

def on_key_press(event, fig, axes, limit_bars, custom_style):
    global current_index, list_signals, current_tf_mode, crosshairs, is_updating, ani, bm_background, ruler_elements, current_custom_symbol
    if is_updating:
        return

    changed = False
    target_symbol = None

    # 1. XỬ LÝ PHÍM ĐIỀU HƯỚNG TRÁI / PHẢI / SPACEBAR (Quay về list gốc)
    if event.key in ['right', 'left', ' ']:
        if list_signals:
            current_custom_symbol = None # Xóa trạng thái coin ngoài list
            
            if event.key == 'right' or event.key == ' ': 
                current_index = (current_index + 1) % len(list_signals)
            elif event.key == 'left': 
                current_index = (current_index - 1) % len(list_signals)
                
            target_symbol = list_signals[current_index]
            changed = True
        else:
            print("\n⚠️ [THÔNG BÁO]: Danh sách trống, không thể chuyển đổi.")
            return

    # 2. XỬ LÝ ĐỔI KHUNG THỜI GIAN (Phím 1 và 2)
    elif event.key in ['1', '2']:
        old_tf = current_tf_mode
        if event.key == '1': current_tf_mode = "H4_H1"
        elif event.key == '2': current_tf_mode = "H1_M5"
        
        if current_tf_mode != old_tf:
            if current_custom_symbol:
                target_symbol = current_custom_symbol
            elif list_signals and current_index < len(list_signals):
                target_symbol = list_signals[current_index]
                
            if target_symbol:
                changed = True

    # 3. NHẤN PHÍM 'n' ĐỂ NHẬP MÃ COIN TÙY CHỈNH MỚI
    elif event.key.lower() == 'n':
        print("\n" + "="*40)
        user_coin = input("👉 Nhập mã coin muốn xem (VD: SOL, BTC, ETH...): ").strip().upper()
        print("="*40)
        
        if user_coin:
            if not user_coin.endswith("USDT") and user_coin != "EXIT":
                user_coin += "USDT"
            
            target_symbol = user_coin
            if list_signals and user_coin in list_signals:
                current_index = list_signals.index(user_coin)
                current_custom_symbol = None  
            else:
                current_custom_symbol = user_coin 
            changed = True

    # Thực thi render lại đồ thị nếu có thay đổi
    if changed and target_symbol:
        try:
            is_updating = True
            if ani and ani.event_source: ani.event_source.stop()
            crosshairs.clear()
            ruler_elements.clear()
            bm_background = None
            for ax in axes.flatten(): ax.clear()
            
            render_charts(fig, axes, target_symbol, limit_bars, custom_style)
        finally:
            is_updating = False
            if ani and ani.event_source: ani.event_source.start()

def run_animation_update(fig, axes, limit_bars, custom_style):
    """ 🎯 HÀM ĐIỀU PHỐI CHU KỲ PHÚT: Nhận biết thông minh đang xem coin nào để cập nhật đúng con đó """
    global list_signals, current_index, current_custom_symbol
    
    # Xác định chính xác coin đang hiển thị thực tế trên màn hình để nạp dữ liệu real-time
    coin_to_update = current_custom_symbol if current_custom_symbol else (list_signals[current_index] if list_signals else None)
    
    if coin_to_update:
        render_charts(fig, axes, coin_to_update, limit_bars, custom_style)

def open_interactive_chart_system(detected_coins, initial_index=0, limit_bars=200):
    global list_signals, current_index, crosshairs, ani, current_custom_symbol
    list_signals = detected_coins
    current_index = initial_index
    current_custom_symbol = None # Reset coin ngoài list khi mở lại hệ thống
    crosshairs.clear()
    
    if not list_signals:
        print("[WARN] No symbols to display.")
        return

    custom_style = mpf.make_mpf_style(
        base_mpf_style='charles',
        marketcolors=mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit', volume='inherit'),
        figcolor='#131722', facecolor='#1c2030', gridcolor='#2a2e39', gridstyle=':'
    )

    fig, axes = plt.subplots(nrows=2, ncols=2, figsize=(16, 9), sharex='col', gridspec_kw={'height_ratios': [3, 1]})
    fig.patch.set_facecolor('#131722')

    fig.canvas.mpl_connect('key_press_event', lambda event: on_key_press(event, fig, axes, limit_bars, custom_style))
    fig.canvas.mpl_connect('motion_notify_event', on_mouse_move)
    fig.canvas.mpl_connect('button_press_event', on_mouse_press)
    fig.canvas.mpl_connect('button_release_event', on_mouse_release)

    render_charts(fig, axes, list_signals[current_index], limit_bars, custom_style)
    
    # 🎯 SỬA ĐỔI GỐC: Trỏ func trực tiếp qua run_animation_update để giải quyết lỗi văng và xẹp đồ thị
    ani = animation.FuncAnimation(
        fig, 
        func=lambda frame: run_animation_update(fig, axes, limit_bars, custom_style), 
        interval=15000, 
        cache_frame_data=False
    )

    print("\n[🎯 HƯỚNG DẪN TƯƠNG TÁC ĐỒ THỊ]:")
    print("  -> Nhấn GIỮ CHUỘT TRÁI và KÉO để bật thước đo biên độ % giá và đếm số nến.")
    print("  -> Nhấn phím MŨI TÊN PHẢI (→) hoặc SPACEBAR để NEXT coin.")
    print("  -> Nhấn phím MŨI TÊN TRÁI (←) để BACK coin.")
    print("  -> Nhấn phím SỐ 1 để xem [ H4 | H1 ] | Nhấn phím SỐ 2 để xem [ H1 | M5 ].")
    print("  -> Nhấn phím SỐ 'n' để nhập nhanh một mã coin ngoài danh sách.")
    print("  📌 Chế độ TỰ ĐỘNG CẬP NHẬT nến mới đang chạy chu kỳ 15 giây/lần.\n")
    
    plt.tight_layout()
    plt.show()

# --- HÀM QUÉT THỊ TRƯỜNG & MAIN MENU ---
def scan_single_symbol(symbol):
    try:
        df_h4 = get_data(symbol, '4h')
        dir_h4 = check_direction_flexible(df_h4, lookback_bars=40)
        if not dir_h4: return None
            
        df_h1 = get_data(symbol, '1h')
        is_ltf_ready = check_ltf_setup(df_h1, dir_h4, max_distance_pct=5)
        clean_symbol = symbol.split(':')[0].replace('/', '')
        
        if is_ltf_ready:
            if dir_h4 == "UP": return ("UP", clean_symbol)
            elif dir_h4 == "DOWN": return ("DOWN", clean_symbol)
    except Exception: pass
    return None

def scan_market():
    global list_signals
    print("Scanning the active Futures market... Please wait.\n")
    start_time = time.time()
    symbols = get_active_trading_symbols()
    if not symbols:
        print("No active trading pairs found. Terminating scan.")
        return
        
    print(f"Found {len(symbols)} active USDT pairs. Starting trend scan...\n")
    list_up, list_down = [], []
    
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(scan_single_symbol, symbol) for symbol in symbols]
        for future in as_completed(futures):
            result = future.result()
            if result:
                direction, clean_symbol = result
                if direction == "UP":
                    list_up.append(clean_symbol); print(f"🔥 SIGNAL LONG: {clean_symbol}")
                elif direction == "DOWN":
                    list_down.append(clean_symbol); print(f"❄️ SIGNAL SHORT: {clean_symbol}")

    list_up.sort()
    list_down.sort()
    list_signals = list_up + list_down
    
    end_time = time.time()
    print("\n======================= SCANNER RESULTS =======================")
    print(f"⏱️ Tổng thời gian quét: {end_time - start_time:.2f} giây")
    print("\n📈 LONG SIGNALS (Sorted A-Z):")
    if list_up:
        for idx, coin in enumerate(list_up, 1): print(f"  [{idx}] {coin}")
    else: print("  (No assets found)")
    print("\n📉 SHORT SIGNALS (Sorted A-Z):")
    if list_down:
        for idx, coin in enumerate(list_down, len(list_up) + 1): print(f"  [{idx}] {coin}")
    else: print("  (No assets found)")
    print("\n===========================================================")

    if not list_signals:
        print("\nNo signals detected. Exiting system.")
        return

    while True:
        print(f"\n[MENU] Enter a number [1-{len(list_signals)}] or type COIN NAME to open Interactive Chart Engine.")
        print("Type 'exit' to quit the application.")
        user_input = input("Your selection: ").strip().upper()

        if user_input == 'EXIT':
            print("System closed. Goodbye!"); break
        selected_index = None
        if user_input.isdigit():
            choice_num = int(user_input)
            if 1 <= choice_num <= len(list_signals): selected_index = choice_num - 1
            else: print(f"[WARN] Invalid number. Choose between 1 and {len(list_signals)}."); continue
        else:
            if user_input in list_signals: selected_index = list_signals.index(user_input)
            else: print("[WARN] Coin not found in the current signal list. Try again."); continue

        if selected_index is not None:
            open_interactive_chart_system(detected_coins=list_signals, initial_index=selected_index, limit_bars=200)

if __name__ == "__main__":
    scan_market()


