"""
Escape Room GM Control Center
Flask + Socket.IO web UI, bridged to the Mosquitto broker on the Pi.

Browser  --Socket.IO-->  this app  --MQTT (localhost:1883)-->  Mosquitto  -->  ESP32 gateways / PLC

Topic contract:
  rr/<room>/<device>/cmd        GM command (QoS 1, never retained)
  rr/<room>/<device>/status     "online" / "offline" (retained, LWT)
  rr/<room>/<device>/heartbeat  anything, sent every ~1 s
  rr/<room>/room/state          current workflow step number, or phase text (retained)
  rr/<room>/room/details        optional detail line under the phase (retained)
"""

import os
import threading
import time

from flask import Flask, render_template
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt


# ------------------------------------------------------------
# SETTINGS  (override with environment variables / .env)
# ------------------------------------------------------------

def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

MQTT_HOST = os.environ.get('MQTT_HOST', 'localhost')
MQTT_PORT = int(os.environ.get('MQTT_PORT', '1883'))
MQTT_USER = os.environ.get('MQTT_USER', 'gm')
MQTT_PASS = os.environ.get('MQTT_PASS', '')

WEB_HOST = os.environ.get('WEB_HOST', '0.0.0.0')
WEB_PORT = int(os.environ.get('WEB_PORT', '5000'))

HEARTBEAT_TIMEOUT = float(os.environ.get('HEARTBEAT_TIMEOUT', '3'))


# ------------------------------------------------------------
# ROOM CONFIGURATION
# code        -> the <room> part of every MQTT topic
# controller  -> MQTT device name of the room's ESP32
# plc         -> MQTT device name the PLC gateway reports as
# ------------------------------------------------------------

ROOMS = {
    "Room 1": {
        "name": "Maintenance Room",
        "code": "mr",
        "controller": "ronnie",
        "plc": "plc",
    },
    "Room 2": {
        "name": "Tron Room",
        "code": "tron",
        "controller": "controller",
        "plc": "plc",
    },
    "Room 3": {
        "name": "Temple Room",
        "code": "ij",
        "controller": "winston",
        "plc": "plc",
    },
    "Room 4": {
        "name": "Haunted House Room",
        "code": "st",
        "controller": "demogorgon",
        "plc": "plc",
    },
}


# ------------------------------------------------------------
# BUTTON -> MQTT MAPPING
# action (from the HTML button)  ->  (device, payload)
# Publishes to rr/<room code>/<device>/cmd
# ------------------------------------------------------------

ACTIONS = {
    "Room 1": {
        "ronnie_power_on": ("ronnie", "power_on"),
        "show_toggle":     ("room", "show_toggle"),
        "phone_toggle":    ("room", "phone_toggle"),
        "audio_toggle":    ("room", "alarm_toggle"),
        "tim_show":        ("room", "tim_show"),
    },
    "Room 2": {
        "voice_above_1":   ("room", "voice1"),
        "voice_above_2":   ("room", "voice2"),
    },
    "Room 3": {
        "tail_animation":  ("monkey", "tail"),
        "temple_door":     ("room", "temple_door"),
    },
    "Room 4": {
        "demogorgon_show_1": ("demogorgon", "show1"),
        "demogorgon_reset":  ("demogorgon", "reset"),
        "demogorgon_show_2": ("demogorgon", "show2"),
    },
}


# ------------------------------------------------------------
# ROOM STATUS
# ------------------------------------------------------------

room_status = {
    room: {
        "controller": False,
        "plc": False,
        "phase": "Waiting",
        "details": "Waiting for game to begin.",
        "step": 0,
    }
    for room in ROOMS
}

# last time each (room, "controller"/"plc") was heard from
last_seen = {}
# devices that explicitly said "offline" (LWT)
reported_offline = set()

broker_connected = False
lock = threading.Lock()

CODE_TO_ROOM = {cfg["code"]: room for room, cfg in ROOMS.items()}


# ------------------------------------------------------------
# FLASK / SOCKET.IO
# ------------------------------------------------------------

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'escape-room-secret')

socketio = SocketIO(app, async_mode='threading')


def push_status():
    with lock:
        snapshot = {r: dict(s) for r, s in room_status.items()}
    socketio.emit('status_update', snapshot)


def push_broker():
    socketio.emit('broker_status', {'connected': broker_connected})


@app.route('/')
def index():
    return render_template('index.html', rooms=ROOMS)


@socketio.on('connect')
def on_browser_connect():
    # New or refreshed GM page gets the current picture right away
    push_broker()
    push_status()


# ------------------------------------------------------------
# MQTT
# ------------------------------------------------------------

def role_for(room, device):
    cfg = ROOMS[room]
    if device == cfg["controller"]:
        return "controller"
    if device == cfg["plc"]:
        return "plc"
    return None


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    global broker_connected
    if reason_code == 0:
        broker_connected = True
        print("MQTT: connected to broker")
        client.subscribe([
            ("rr/+/+/status", 1),
            ("rr/+/+/heartbeat", 0),
            ("rr/+/room/state", 1),
            ("rr/+/room/details", 1),
        ])
    else:
        broker_connected = False
        print(f"MQTT: connect refused ({reason_code}) - check MQTT_USER / MQTT_PASS")
    push_broker()


def on_mqtt_disconnect(client, userdata, flags, reason_code, properties=None):
    global broker_connected
    broker_connected = False
    print(f"MQTT: disconnected ({reason_code}), retrying...")
    with lock:
        for s in room_status.values():
            s["controller"] = False
            s["plc"] = False
    push_broker()
    push_status()


def on_mqtt_message(client, userdata, msg):
    parts = msg.topic.split('/')
    if len(parts) != 4 or parts[0] != 'rr':
        return
    _, code, device, kind = parts
    room = CODE_TO_ROOM.get(code)
    if room is None:
        return
    payload = msg.payload.decode(errors='replace').strip()

    changed = False
    with lock:
        status = room_status[room]

        if kind in ('heartbeat', 'status'):
            role = role_for(room, device)
            if role:
                key = (room, role)
                if kind == 'status' and payload.lower() == 'offline':
                    reported_offline.add(key)
                    last_seen.pop(key, None)
                    if status[role]:
                        status[role] = False
                        changed = True
                else:
                    reported_offline.discard(key)
                    last_seen[key] = time.monotonic()
                    if not status[role]:
                        status[role] = True
                        changed = True

        elif device == 'room' and kind == 'state':
            if payload.isdigit():
                status["step"] = int(payload)
                status["phase"] = f"Step {payload}"
            else:
                status["step"] = 0
                status["phase"] = payload or "Waiting"
            changed = True

        elif device == 'room' and kind == 'details':
            status["details"] = payload
            changed = True

    if changed:
        push_status()


mqttc = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id=f"gm-server-{os.getpid()}",
)
mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
mqttc.on_connect = on_mqtt_connect
mqttc.on_disconnect = on_mqtt_disconnect
mqttc.on_message = on_mqtt_message
mqttc.reconnect_delay_set(min_delay=1, max_delay=5)


def heartbeat_watchdog():
    """Mark a device offline if no heartbeat within HEARTBEAT_TIMEOUT seconds."""
    while True:
        socketio.sleep(0.5)
        now = time.monotonic()
        changed = False
        with lock:
            for room, status in room_status.items():
                for role in ("controller", "plc"):
                    seen = last_seen.get((room, role))
                    if status[role] and (seen is None or now - seen > HEARTBEAT_TIMEOUT):
                        status[role] = False
                        changed = True
        if changed:
            push_status()


def publish_cmd(room, device, payload):
    """Send a GM command. Returns (ok, message)."""
    if not broker_connected:
        return False, "Broker offline - command NOT sent"
    topic = f"rr/{ROOMS[room]['code']}/{device}/cmd"
    info = mqttc.publish(topic, payload, qos=1, retain=False)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        return False, f"Publish failed ({info.rc})"
    print(f"CMD  {topic}  {payload}")
    return True, f"Sent {payload} to {ROOMS[room]['name']}"


# ------------------------------------------------------------
# GM COMMANDS FROM THE BROWSER
# ------------------------------------------------------------

@socketio.on('trigger_action')
def handle_action(data):
    room = data.get('room')
    action = data.get('action')

    if room not in ROOMS:
        return {'ok': False, 'msg': f"Unknown room: {room}"}

    mapping = ACTIONS.get(room, {}).get(action)
    if mapping is None:
        print(f"Unmapped action: {room} / {action}")
        return {'ok': False, 'msg': f"No MQTT mapping for '{action}' - add it to ACTIONS in app.py"}

    device, payload = mapping
    ok, msg = publish_cmd(room, device, payload)
    return {'ok': ok, 'msg': msg}


@socketio.on('reset_room')
def handle_reset_room(data):
    room = data.get('room')
    if room not in ROOMS:
        return {'ok': False, 'msg': f"Unknown room: {room}"}

    ok, msg = publish_cmd(room, 'room', 'reset')
    # The PLC is the source of truth: it publishes the new state after it resets.
    return {'ok': ok, 'msg': msg}


# ------------------------------------------------------------
# START SERVER
# ------------------------------------------------------------

if __name__ == '__main__':
    print()
    print("=" * 60)
    print("ESCAPE ROOM GM CONTROL CENTER")
    print("=" * 60)
    print(f"Web:    http://{WEB_HOST}:{WEB_PORT}")
    print(f"Broker: {MQTT_HOST}:{MQTT_PORT} as '{MQTT_USER}'")
    print("=" * 60)
    print()

    if not MQTT_PASS:
        print("WARNING: MQTT_PASS is empty - set it in .env")

    mqttc.connect_async(MQTT_HOST, MQTT_PORT, keepalive=10)
    mqttc.loop_start()
    socketio.start_background_task(heartbeat_watchdog)

    socketio.run(
        app,
        host=WEB_HOST,
        port=WEB_PORT,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
