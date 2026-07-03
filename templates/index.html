from flask import Flask, render_template, request, jsonify
import json
import os

app = Flask(__name__)

DATA_FILE = "mt4_live.json"  # Xogta MT4 ayaa halkan lagu kaydin doonaa

def get_current_data():
    """Soo celi xogta hadda kaydsan, ama default haddii aan file jirin."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    # Fallback: xog eber ah oo muujinaysa in aan weli xog la helin
    return {
        "balance": 0, "equity": 0, "profit": 0,
        "win_rate": 0, "drawdown": 0, "open_trades_count": 0,
        "spread": None, "update_time": "Sugaya xog MT4...",
        "open_trades": [],
        "closed_loss_trades": [],
        "symbol_results": [],
        "risk_percent": 2.0,
        "lot_size": 0.01,
        "current_strategy": "trend",
        "strategies": [
            {"value": "scalping", "name": "Scalping Fast"},
            {"value": "trend", "name": "Trend Follower"},
            {"value": "grid", "name": "Grid Hedged"},
            {"value": "martingale", "name": "Martingale Safe"}
        ]
    }

@app.route('/')
def dashboard():
    data = get_current_data()
    return render_template('index.html', **data)

@app.route('/live_data')
def live_data():
    """JSON-ka ay JavaScript-ku isticmaasho si uu u cusboonaysiiyo bogga."""
    return jsonify(get_current_data())

@app.route('/update_data', methods=['POST'])
def update_data():
    """Hel xog cusub MT4 oo ku kaydi fayl."""
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "JSON required"}), 400
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f)
        return jsonify({"status": "ok", "message": "Data updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Haddii aad Render isticmaalayso, wuxuu isagu port galiyaa, laakiin tan waa local
    app.run(host='0.0.0.0', port=5000)
