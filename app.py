from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import os
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = 'moha-pro-admin-2024'
socketio = SocketIO(app, cors_allowed_origins="*")

# Xogta ugu dambeysay
latest_data = {
    "balance": 0, "equity": 0, "profit": 0,
    "winrate": 0, "drawdown": 0, "opentrades": 0,
    "strategy": "---", "trades": [], "signals": [],
    "trading": True
}

# Amarka ugu dambeeyay (MT4 wuu akhriyi doonaa)
pending_commands = []

@app.route('/')
def dashboard():
    return render_template('index.html')

@app.route('/admin')
def admin():
    return render_template('admin.html')

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

@app.route('/command', methods=['POST'])
def command():
    global pending_commands
    try:
        data = request.get_json()
        cmd = data.get('command', '')
        pending_commands.append(data)
        add_log(f"Command received: {cmd}")
        return jsonify({"status": "ok", "message": f"Amar la qaatay: {cmd}"}), 200
    except:
        return jsonify({"status": "error", "message": "Khalad"}), 400

@app.route('/api/commands', methods=['GET'])
def get_commands():
    global pending_commands
    cmds = pending_commands.copy()
    pending_commands.clear()
    return jsonify({"commands": cmds}), 200

@app.route('/api/data', methods=['GET'])
def get_data():
    return jsonify(latest_data), 200

@socketio.on('connect')
def handle_connect():
    emit('data_update', latest_data)

# Log system
log_messages = []

def add_log(msg):
    global log_messages
    log_messages.append(msg)
    if len(log_messages) > 100:
        log_messages.pop(0)

@app.route('/api/logs', methods=['GET'])
def get_logs():
    return jsonify({"logs": log_messages}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    socketio.run(app, host='0.0.0.0', port=port)
