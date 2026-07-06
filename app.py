from flask import Flask, render_template, request, jsonify
from datetime import datetime

app = Flask(__name__)

# FURAHA SIRTA AH - Waa inuu la mid yahay kan EA-ga (Cloud_Auth_Token)
SECRET_TOKEN = "MohaPro_Live_2026_MySecret"  # Fadlan beddel adiga kuu gaar ah

# Xogta bot-ka (waxaa lagu keydin doonaa memory-ka)
bot_data = {
    "balance": 0.0,
    "equity": 0.0,
    "profit": 0.0,
    "winrate": 0.0,
    "drawdown": 0.0,
    "opentrades": 0,
    "status": "STOPPED",
    "strategy": "SR",
    "lastUpdate": None
}

# Amarka la sugayo (bot-ku wuxuu soo eegayaa midkan)
pending_command = "NONE"

# ============================================
# 1. BOT-KU WUXUU SOO DIRAA XOGTA (POST /update)
# ============================================
@app.route('/update', methods=['POST'])
def update_bot_data():
    global bot_data
    data = request.get_json()
    
    # Hubi token-ka (Security)
    token = data.get('token')
    if token != SECRET_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    
    # Cusboonaysii xogta
    bot_data["balance"] = float(data.get('balance', 0))
    bot_data["equity"] = float(data.get('equity', 0))
    bot_data["profit"] = float(data.get('profit', 0))
    bot_data["winrate"] = float(data.get('winrate', 0))
    bot_data["drawdown"] = float(data.get('drawdown', 0))
    bot_data["opentrades"] = int(data.get('opentrades', 0))
    bot_data["lastUpdate"] = datetime.now().isoformat()
    
    return jsonify({"status": "ok"})

# ============================================
# 2. BOT-KU WUXUU SOO EEGAYAA AMARKA (GET /api/commands)
# ============================================
@app.route('/api/commands', methods=['GET'])
def get_commands():
    global pending_command
    
    # Hubi Authorization header
    auth_header = request.headers.get('Authorization')
    if not auth_header or auth_header != f"Bearer {SECRET_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401
    
    # Soo celi amarka hadda ku jira, kadibna nadiifi
    cmd = pending_command
    pending_command = "NONE"  # Marka la akhriyo waa la tirtiraa si aan isku soo noqnoqon
    
    # Bot-ku wuxuu rabaa inuu arko token-ka si uu u xaqiijiyo (signed commands)
    return jsonify({
        "token": SECRET_TOKEN,
        "cmd": cmd
    })

# ============================================
# 3. WAP-KU WUXUU SOO EEGAYAA XOGTA (GET /api/status)
# ============================================
@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify(bot_data)

# ============================================
# 4. ADIGA (WAP) AYAD U DIRTA AMAR (POST /api/send-command)
# ============================================
@app.route('/api/send-command', methods=['POST'])
def send_command():
    global pending_command
    data = request.get_json()
    command = data.get('command')
    
    if not command:
        return jsonify({"error": "Command required"}), 400
    
    pending_command = command
    return jsonify({"status": "ok", "command": command})

# ============================================
# 5. WADADDA BILOW (/) - Soo bandhig WAP-ka
# ============================================
@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    # Render ama VPS-ka waxay isticmaalaan PORT-ka deegaanka
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
