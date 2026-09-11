"""
MOHA PRO - Cloud Dashboard Backend (v3, multi-bot)
--------------------------------------------------
Isbeddelka v3:
  - LABA BOT AMA KA BADAN: bot kastaa wuxuu diraa `"bot":"<magac>"`. Xogtoodu
    gebi ahaanba way kala go'an tahay - state, amarro, symbols.
  - Dashboard-ku wuxuu leeyahay bot switcher: "Dhammaan" ama mid gaar ah.
  - Amarku wuxuu u socdaa BOOTKA la doortay oo keliya.

Endpoints:
  POST /update                        - bootka -> xogta   (body: token, bot, ...)
  GET  /state?token=&bot=             - dashboard <- xogta bot gaar ah
  GET  /state?token=                  - dashboard <- liiska botyada oo dhan
  GET  /api/commands?token=&bot=      - bootka <- amarka soo socda
  POST /admin/command                 - dashboard -> dir amar (token, bot, command)
  GET  /diag                          - sababta DEMO
  GET  /signals, /health
"""
import os
import time
import json
import urllib.request
import urllib.parse
from flask import Flask, request, jsonify, Response

app = Flask(__name__)