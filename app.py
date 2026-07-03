from flask import Flask, render_template, request, jsonify
import json

app = Flask(__name__)

# Tani waa xogta tusaalaha ah - beddel tan oo akhri xogta dhabta ah
def get_mt4_data():
    # Halkan waxaad ka akhri kartaa faylka JSON, database, ama MT4 API
    # Tusaale: akhri faylka 'mt4_data.json'
    # with open('mt4_data.json') as f:
    #     data = json.load(f)
    # Haddii aad haysato database: 
    # data = db.fetch_all()
    
    # Qaab-dhismeedka xogta:
    data = {
        "balance": 4912.82,
        "equity": 4914.23,
        "profit": 0.00,
        "win_rate": 100.0,
        "drawdown": 0.0,
        "open_trades_count": 1,
        "spread": None,  # ama ''
        "update_time": "13:40:23",  # ama waqti toos ah oo server-ka laga helo
        "open_trades": [
            {
                "symbol": "EURUSD",
                "type": "Buy",
                "lot": 0.01,
                "open_price": 1.0850,
                "tp": 1.0870,
                "sl": 1.0830,
                "profit": 1.23,
                "swap": -0.15
            }
        ],
        "closed_loss_trades": [
            {
                "symbol": "GBPUSD",
                "type": "Sell",
                "lot": 0.02,
                "close_time": "2026-07-03 09:15",
                "open_price": 1.2710,
                "close_price": 1.2732,
                "loss": -4.40
            },
            {
                "symbol": "USDCAD",
                "type": "Buy",
                "lot": 0.01,
                "close_time": "2026-07-02 14:00",
                "open_price": 1.3680,
                "close_price": 1.3645,
                "loss": -3.50
            }
        ],
        "symbol_results": [
            {"pair": "EURUSD", "trades": 12, "wins": 7, "pnl": 45.20},
            {"pair": "GBPUSD", "trades": 5, "wins": 2, "pnl": -12.50},
            {"pair": "XAUUSD", "trades": 3, "wins": 3, "pnl": 89.00},
            {"pair": "USDCAD", "trades": 1, "wins": 0, "pnl": -5.30}
        ],
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
    return data

@app.route('/')
def dashboard():
    data = get_mt4_data()
    return render_template('index.html', **data)

@app.route('/save_settings', methods=['POST'])
def save_settings():
    # Halkan waxaad ku keydisaa dejinta (fayl, database, iwm.)
    settings = request.get_json()
    risk = settings.get('risk_percent')
    lot = settings.get('lot_size')
    strategy = settings.get('strategy')
    # Tusaale: ku qor faylka JSON
    with open('settings.json', 'w') as f:
        json.dump(settings, f)
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
