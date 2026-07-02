from flask import Flask, request, jsonify, render_template
from datetime import datetime

app = Flask(__name__)

# Xogta ugu dambeysay ee laga helo EA
latest_data = {
    "balance": 0,
    "equity": 0,
    "profit": 0,
    "winrate": 0,
    "drawdown": 0,
    "opentrades": 0,
    "spread": 0,
    "lastUpdate": ""
}

# Amarka hadda jira (oo uu akhriyo EA, ka dibna la tirtiro)
current_command = ""

@app.route('/')
def dashboard():
    return render_template('index.html')

# EA waxay u soo diri doontaa xogta halkan (POST)
@app.route('/update', methods=['POST'])
def update():
    data = request.get_json()
    if data:
        latest_data.update(data)
        latest_data['lastUpdate'] = datetime.now().strftime("%H:%M:%S")
        print("Xog cusub laga helay EA:", latest_data)
    return "OK"

# EA waxay ka akhrisan doontaa amar cusub (GET)
@app.route('/api/commands')
def get_command():
    global current_command
    cmd = current_command
    current_command = ""  # tirtir marka la akhriyo
    return cmd

# Dashboard-ka ayaa soo jiidan doona xogta (GET)
@app.route('/api/data')
def get_data():
    return jsonify(latest_data)

# Dashboard-ka ayaa ku shubi doona amar cusub (POST)
@app.route('/api/setcommand', methods=['POST'])
def set_command():
    global current_command
    data = request.get_json()
    if data and 'command' in data:
        current_command = data['command']
        print("Amar cusub:", current_command)
    return "OK"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
