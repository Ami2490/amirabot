import time
import requests
import hmac
from hashlib import sha256
import json
import pandas as pd
import sys
import os

# ==========================================
# 1. CONFIGURACIÓN
# ==========================================
SYMBOL = "WLD-USDT"
INTERVAL = "1m"
LEVERAGE = 10

APIURL = "https://open-api-vst.bingx.com"
APIKEY = os.environ["APIKEY"]
SECRETKEY = os.environ["SECRETKEY"]

EMA_FAST = 13
EMA_SLOW = 95
EMA_TREND = 30

TP1_PCT = 0.011   # 1.1% (Meta para Eureka)
TS_FASE1 = 0.072   # 8.0% (Hard SL Inicial)
TS_FASE2 = 0.015  # 1.1% (Trailing Final Modificado)

position_open = False
cambiando_fase = False 
server_time_offset = 0

# ==========================================
# 2. CONEXIÓN (CON RECONEXIÓN AUTOMÁTICA)
# ==========================================
def sync_server_time():
    global server_time_offset
    try:
        t_start = int(time.time() * 1000)
        res = requests.get(f"{APIURL}/openApi/swap/v2/server/time", timeout=5).json()
        t_end = int(time.time() * 1000)
        server_time_offset = res['data']['serverTime'] - ((t_start + t_end) // 2)
    except: server_time_offset = 0

def get_timestamp():
    return str(int(time.time() * 1000) + server_time_offset)

def get_sign(api_secret, payload):
    return hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()

def send_request(method, path, paramsStr, payload=None, verbose=False):
    try:
        signature = get_sign(SECRETKEY, paramsStr)
        url = f"{APIURL}{path}?{paramsStr}&signature={signature}"
        headers = {'X-BX-APIKEY': APIKEY}
        response = requests.request(method, url, headers=headers, data=payload, timeout=10)
        res_json = response.json()
        
        if res_json.get('code') == 109400: # Timestamp error
            sync_server_time(); return response.text 
        
        # LOGS: Solo si es orden o error
        is_order = "trade/order" in path or "closeAll" in path
        if is_order or verbose or res_json.get('code') != 0:
            print(f"\n[API] {path} -> {response.text}")
            
        return response.text
    except Exception as e:
        return json.dumps({"code": -999, "msg": "Network Error"})

def parseParam(paramsMap):
    return "&".join([f"{k}={paramsMap[k]}" for k in sorted(paramsMap)])

# ==========================================
# 3. DATOS (TOLERANCIA A FALLOS)
# ==========================================
def get_balance():
    params = {"timestamp": get_timestamp()}
    res = json.loads(send_request("GET", '/openApi/swap/v3/user/balance', parseParam(params)))
    if res.get('code') == 0:
        for asset in res['data']:
            if asset['asset'] == 'VST': return float(asset['balance'])
    return 0.0

def get_price():
    params = {"symbol": SYMBOL, "timestamp": get_timestamp()}
    res = json.loads(send_request("GET", '/openApi/swap/v1/ticker/price', parseParam(params)))
    if res.get('code') == 0: return float(res['data']['price'])
    return None

def get_pos():
    params = {"symbol": SYMBOL, "timestamp": get_timestamp()}
    res_text = send_request("GET", '/openApi/swap/v2/user/positions', parseParam(params))
    try:
        res = json.loads(res_text)
        if res.get('code') == 0:
            if res.get('data'):
                pos = res['data'][0]
                if abs(float(pos['positionAmt'])) > 0.0001: return pos
            return None 
        else: return 'ERR' 
    except: return 'ERR'

def get_orders_count():
    params = {"symbol": SYMBOL, "timestamp": get_timestamp()}
    res = json.loads(send_request("GET", '/openApi/swap/v2/trade/openOrders', parseParam(params)))
    if res.get('code') == 0: return len(res['data']['orders']), res['data']['orders']
    return -1, [] # -1 = Error de red

# ==========================================
# 4. ACCIONES Y SEGURIDAD CRÍTICA
# ==========================================
def place_market_order(side, quantity, close=False):
    qty_int = int(quantity)
    pos_side = "LONG" if (side == "BUY" and not close) or (side == "SELL" and close) else "SHORT"
    print(f"\n>>> Enviando Orden {side} (Cierre: {close}) Qty: {qty_int}...")
    paramsMap = {
        "symbol": SYMBOL, "side": side, "positionSide": pos_side,
        "type": "MARKET", "quantity": str(qty_int), "timestamp": get_timestamp()
    }
    res = send_request("POST", '/openApi/swap/v2/trade/order', parseParam(paramsMap), {}, verbose=True)
    return '"code":0' in res

def place_hard_tp(side, pos_side, qty, price):
    print(f"\n>>> Intentando poner TP en Exchange @ {price}...")
    paramsMap = {
        "symbol": SYMBOL, "side": side, "positionSide": pos_side,
        "type": "TAKE_PROFIT_MARKET", 
        "quantity": str(int(qty)),
        "stopPrice": str(round(price, 6)), 
        "timestamp": get_timestamp()
    }
    res = send_request("POST", '/openApi/swap/v2/trade/order', parseParam(paramsMap), {}, verbose=True)
    return '"code":0' in res

def place_ts(side, pos_side, qty, rate):
    print(f"\n>>> Intentando poner Trailing Stop {rate*100}%...")
    paramsMap = {
        "symbol": SYMBOL, "side": side, "positionSide": pos_side,
        "type": "TRAILING_TP_SL", "quantity": str(int(qty)),
        "priceRate": str(rate), "timestamp": get_timestamp()
    }
    res = send_request("POST", '/openApi/swap/v2/trade/order', parseParam(paramsMap), {}, verbose=True)
    return '"code":0' in res

def cancel_order(order_id):
    print(f"\n>>> Borrando Orden ID: {order_id}...")
    params = {"symbol": SYMBOL, "orderId": str(order_id), "timestamp": get_timestamp()}
    return '"code":0' in send_request("DELETE", '/openApi/swap/v2/trade/order', parseParam(params), verbose=True)

def close_all():
    print("\n[ALERTA ROJA] CERRANDO TODO POR SEGURIDAD...")
    ts = get_timestamp()
    send_request("POST", "/openApi/swap/v2/trade/closeAllPositions", parseParam({"symbol": SYMBOL, "timestamp": ts}), verbose=True)
    send_request("POST", "/openApi/swap/v2/trade/cancelAllOrders", parseParam({"symbol": SYMBOL, "timestamp": ts}), verbose=True)

# ==========================================
# 5. MONITOR "PACIENCIA INFINITA"
# ==========================================
def monitor(side, entry_p, initial_qty):
    tp1_price = entry_p * (1 + TP1_PCT) if side == "BUY" else entry_p * (1 - TP1_PCT)
    
    print(f"\n{'='*40}")
    print(f" MONITOR ACTIVO: {side} @ {entry_p:.6f}")
    print(f" TP: {tp1_price:.6f} | SL: {TS_FASE1*100}% (En Exchange)")
    print(f"{'='*40}")
    
    tp1_hit = False
    exit_s = "SELL" if side == "BUY" else "BUY"
    pos_s = "LONG" if side == "BUY" else "SHORT"
    global position_open, cambiando_fase

    while position_open:
        try:
            p = get_price(); pos = get_pos()
            
            # --- PACIENCIA ANTE ERROR DE RED ---
            if p is None or pos == 'ERR': 
                sys.stdout.write(f"\r[RED] Conexión inestable... Esperando...".ljust(60))
                time.sleep(3)
                continue # NO CERRAMOS, SOLO ESPERAMOS
            
            # --- CIERRE CONFIRMADO POR EXCHANGE ---
            if pos is None: 
                print(f"\n[INFO] Posición cerrada (TP o Trailing ejecutado).")
                position_open = False; break

            # --- LÓGICA EUREKA (MODIFICACIÓN) ---
            if not tp1_hit:
                reached = (side == "BUY" and p >= tp1_price) or (side == "SELL" and p <= tp1_price)
                if reached:
                    print(f"\n[✔] EUREKA! Precio Tocado. Modificando...")
                    cambiando_fase = True
                    
                    # 1. BORRAR TRAILING 8%
                    _, orders = get_orders_count()
                    for o in orders:
                        if o['type'] == 'TRAILING_TP_SL': cancel_order(o['orderId'])
                    time.sleep(1.5)
                    
                    # 2. VENDER 70%
                    if place_market_order(exit_s, initial_qty * 0.7, close=True):
                        time.sleep(1.5)
                        
                        # 3. PONER TRAILING 1.1% (RESTO)
                        new_pos = get_pos()
                        if new_pos != 'ERR' and new_pos is not None:
                            rem_qty = abs(float(new_pos['positionAmt']))
                            if place_ts(exit_s, pos_s, rem_qty, TS_FASE2):
                                tp1_hit = True
                                print(">>> CAMBIO COMPLETADO. Ganancia asegurada.")
                    cambiando_fase = False

            # --- SIN PÁNICO DE CIERRE ---
            # El monitor ya NO cierra por falta de órdenes. 
            # Asume que si la red falla, las órdenes siguen en BingX.

            pnl = ((p - entry_p)/entry_p)*100*LEVERAGE if side=="BUY" else ((entry_p - p)/entry_p)*100*LEVERAGE
            sys.stdout.write(f"\r[VIVO] P: {p:.6f} | PnL: {pnl:.2f}% | Fase: {'2 (1.1%)' if tp1_hit else '1 (8.0%)'}".ljust(60))
            sys.stdout.flush()
            time.sleep(1)
            
        except Exception as e:
            # Si hay crash de python, tampoco cerramos. Esperamos.
            print(f"\n[Err Monitor] {e} - Reintentando..."); time.sleep(2)

# ==========================================
# 6. MAIN (PROTOCOLO BLINDADO DE ENTRADA)
# ==========================================
def main():
    global position_open
    print("=== SNIPER V9 (PROTOCOLO BLINDADO) ===")
    sync_server_time()
    
    last_ema_13 = None
    last_ema_95 = None

    while True:
        try:
            # 1. RECUPERACIÓN (Si se reinicia el bot)
            pos = get_pos()
            if pos != 'ERR' and pos is not None:
                position_open = True
                print("\n[INFO] Posición detectada. Retomando monitoreo...")
                monitor("BUY" if float(pos['positionAmt']) > 0 else "SELL", float(pos['avgPrice']), abs(float(pos['positionAmt'])))
                continue

            # 2. ESCANEO
            bal = get_balance()
            url = f"{APIURL}/openApi/swap/v3/quote/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=100"
            try: res_k = requests.get(url, timeout=5).json()
            except: time.sleep(3); continue

            if res_k['code'] == 0:
                df = pd.DataFrame(res_k['data'])
                df = df.iloc[::-1].reset_index(drop=True)
                df['close'] = df['close'].astype(float)
                e13 = df['close'].ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
                e95 = df['close'].ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
                e30 = df['close'].ewm(span=EMA_TREND, adjust=False).mean().iloc[-1]
                p = df['close'].iloc[-1]

                sys.stdout.write(f"\r[SCAN] Bal: {int(bal)} | P: {p:.6f} | E13: {e13:.6f} | E95: {e95:.6f}".ljust(60))
                sys.stdout.flush()

                if last_ema_13 is not None:
                    c_up = (last_ema_13 <= last_ema_95) and (e13 > e95)
                    c_down = (last_ema_13 >= last_ema_95) and (e13 < e95)
                    if (c_up or c_down) and not position_open:
                        if (c_up and e13 > e30 and e95 > e30) or (c_down and e13 < e30 and e95 < e30):
                            side = "BUY" if c_up else "SELL"
                            print(f"\n\n[!!!] SEÑAL {side} @ {p:.6f}")
                            curr_p = get_price()
                            qty = (bal * 0.90 * LEVERAGE) / curr_p
                            
                            # --- PROTOCOLO DE APERTURA SEGURA ---
                            if place_market_order(side, qty):
                                print("[INFO] Orden enviada. Asegurando...")
                                time.sleep(1)
                                
                                opp_side = "SELL" if side == "BUY" else "BUY"
                                pos_s = "LONG" if side == "BUY" else "SHORT"
                                tp_val = curr_p * (1 + TP1_PCT) if side == "BUY" else curr_p * (1 - TP1_PCT)
                                
                                # INTENTO 1: PONER PROTECCIONES
                                tp_ok = place_hard_tp(opp_side, pos_s, qty * 0.7, tp_val)
                                ts_ok = place_ts(opp_side, pos_s, qty, TS_FASE1)
                                
                                # REINTENTO SI FALLA EL TRAILING (CRITICO)
                                if not ts_ok:
                                    print("[ALERTA] Falló Trailing. Reintentando (2/3)...")
                                    time.sleep(2)
                                    ts_ok = place_ts(opp_side, pos_s, qty, TS_FASE1)
                                
                                if not ts_ok:
                                    print("[ALERTA] Falló Trailing. Reintentando (3/3)...")
                                    time.sleep(2)
                                    ts_ok = place_ts(opp_side, pos_s, qty, TS_FASE1)
                                
                                # SI FALLA 3 VECES -> CIERRE DE EMERGENCIA Y APAGADO
                                if not ts_ok:
                                    print("\n[CRITICAL ERROR] NO SE PUDO PROTEGER LA OPERACIÓN.")
                                    print(">>> CERRANDO TODO Y DETENIENDO BOT PARA REVISIÓN MANUAL.")
                                    close_all()
                                    sys.exit() # SE APAGA EL BOT
                                else:
                                    print("[OK] Operación Protegida. Iniciando Monitor...")
                                    position_open = True
                                    monitor(side, curr_p, qty)
                                    last_ema_13 = None
                            else:
                                print("[ERROR] Falló la entrada (Margen/API). Esperando...")
                                time.sleep(5)

                last_ema_13, last_ema_95 = e13, e95
            time.sleep(2)
        except Exception as e:
            time.sleep(5); sync_server_time()

if __name__ == "__main__":
    main()