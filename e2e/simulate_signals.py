import json
import time
import zmq
from datetime import datetime, timezone

# Simulation settings
ZMQ_ADDR = "tcp://127.0.0.1:5555"
TOPIC = "SIGNAL"

def send_signal(socket, action, symbol="XAUUSD", price=2350.0, quantity=0.1, sl=None, tp1=None, tp2=None, is_running=False):
    payload = {
        "signal_id": f"sim_{int(time.time())}_{action}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "symbol": symbol,
        "price": price,
        "quantity": quantity,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "is_running": is_running,
        "risk_percent": 1.0
    }
    
    message = f"{TOPIC}|{json.dumps(payload)}"
    print(f"Sending: {message}")
    socket.send_string(message)

def main():
    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    socket.bind(ZMQ_ADDR)
    
    print(f"ZMQ Publisher bound to {ZMQ_ADDR}")
    print("Waiting for subscribers to connect...")
    time.sleep(2)  # Give subscribers time to connect
    
    # 1. Entry LONG
    send_signal(socket, "LONG", sl=2340.0, tp1=2370.0, tp2=2390.0)
    time.sleep(1)
    
    # 2. Update SL (R_SL)
    send_signal(socket, "R_SL", price=2355.0, sl=2350.5, is_running=True)
    time.sleep(1)
    
    # 3. Hit TP1
    send_signal(socket, "TP1", price=2370.0, quantity=0.05, sl=2350.5, is_running=True)
    time.sleep(1)
    
    # 4. Hit SL (Full close)
    send_signal(socket, "SL", price=2350.5, quantity=0.05, is_running=False)
    
    print("All simulated signals sent.")
    socket.close()
    context.term()

if __name__ == "__main__":
    main()
