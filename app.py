from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import os

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

latest_data = {
    "balance": 0, "equity": 0, "profit": 0,
    "winrate": 0, "drawdown": 0, "opentrades": 0,
    "strategy": "---", "trades": [], "signals": []
}

@app.route('/')
def dashboard():
    return render_template('index.html')

@app.route('/update', methods=['POST'])
def update():
    global latest_data
    try:
        data = request.get_json()
        if data:
            latest_data = data
            socketio.emit('data_update', latest_data)
            return jsonify({"status": "ok"}), 200
    except:
        pass
    return jsonify({"status": "error"}), 400

@socketio.on('connect')
def handle_connect():
    emit('data_update', latest_data)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    socketio.run(app, host='0.0.0.0', port=port)
