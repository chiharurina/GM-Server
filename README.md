# GM-Server

Game Master control page for the Retro Rewind escape room. GMs use it to trigger shows and effects, reset rooms, and see which devices are online.

## How it works

```
Browser  --Socket.IO-->  app.py (Flask)  --MQTT-->  Mosquitto  -->  ESP32s / PLC
```

- **`templates/index.html`** is the GM page. Buttons call `sendAction(room, action)` or `resetRoom(room)` and send them to the server over Socket.IO.
- **`app.py`** is a Flask + Flask-SocketIO server and an MQTT client (paho-mqtt). It turns each button press into an MQTT command and pushes device status back to every open GM page.
- **Mosquitto** is the message broker. The ESP32s and the PLC gateway subscribe to commands and publish status.

The browser never talks to hardware or the broker directly. The PLC gets the final say on whether a command is allowed to run.

## Project structure

```
GM-Server/
├── app.py               # Web server + MQTT bridge
├── templates/
│   └── index.html       # GM control page
├── static/
│   └── socket.io.min.js # Socket.IO client, served locally (works offline)
├── fake_devices.py      # Simulates ESP32s/PLC for testing
├── requirements.txt
├── .env.example         # Config template
└── gm-server.service    # systemd unit for running on boot
```

## Configuration

Settings are read from a `.env` file next to `app.py`. Copy `.env.example` to `.env` to start. Don't commit `.env`.

| Variable | Default | Description |
|---|---|---|
| `MQTT_HOST` | `localhost` | Broker address |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USER` | `gm` | Broker username |
| `MQTT_PASS` | *(empty)* | Broker password |
| `WEB_HOST` | `0.0.0.0` | Address the web page listens on |
| `WEB_PORT` | `5000` | Web page port |
| `HEARTBEAT_TIMEOUT` | `3` | Seconds without a heartbeat before a device shows OFFLINE |

## Rooms and buttons

Both are defined at the top of `app.py`.

**`ROOMS`** sets each room's MQTT code and the device names its status lights follow:

| Room | Code | Controller light | PLC light |
|---|---|---|---|
| Maintenance Room | `mr` | `ronnie` | `plc` |
| Tron Room | `tron` | `controller` | `plc` |
| Temple Room | `ij` | `winston` | `plc` |
| Haunted House Room | `st` | `demogorgon` | `plc` |

**`ACTIONS`** maps each button to a device and a command:

| Room | Button action | Publishes to | Payload |
|---|---|---|---|
| mr | `ronnie_power_on` | `rr/mr/ronnie/cmd` | `power_on` |
| mr | `show_toggle` | `rr/mr/room/cmd` | `show_toggle` |
| mr | `phone_toggle` | `rr/mr/room/cmd` | `phone_toggle` |
| mr | `audio_toggle` | `rr/mr/room/cmd` | `alarm_toggle` |
| mr | `tim_show` | `rr/mr/room/cmd` | `tim_show` |
| tron | `voice_above_1` / `voice_above_2` | `rr/tron/room/cmd` | `voice1` / `voice2` |
| ij | `tail_animation` | `rr/ij/monkey/cmd` | `tail` |
| ij | `temple_door` | `rr/ij/room/cmd` | `temple_door` |
| st | `demogorgon_show_1` / `_reset` / `_show_2` | `rr/st/demogorgon/cmd` | `show1` / `reset` / `show2` |
| any | Reset room button | `rr/<code>/room/cmd` | `reset` |

### Adding a button

1. Add the button in `index.html`:
   ```html
   <button class="control-btn" onclick="sendAction('Room 4', 'lights_off')">LIGHTS OFF</button>
   ```
2. Map it in `ACTIONS` in `app.py`:
   ```python
   "Room 4": {
       ...
       "lights_off": ("room", "lights_off"),
   },
   ```
3. Restart the server.

A button without a mapping shows a red popup instead of silently doing nothing.

## MQTT topics

Everything lives under `rr/<room>/<device>/...`.

| Topic | Published by | Payload | Retained |
|---|---|---|---|
| `rr/<room>/<device>/cmd` | GM server | Command word, e.g. `show1` | No (QoS 1) |
| `rr/<room>/<device>/heartbeat` | Each device | Anything, about every 1 s | No |
| `rr/<room>/<device>/status` | Each device | `online`, or `offline` as the last-will message | Yes |
| `rr/<room>/room/state` | PLC gateway | Current workflow step number (e.g. `3`) or phase text | Yes |
| `rr/<room>/room/details` | PLC gateway | A line of text shown under the phase | Yes |

Commands are never retained, so they can't replay when a device reconnects.

## What the page shows

- **Badge (top right):** CONNECTED, BROKER: OFFLINE, or SERVER: OFFLINE.
- **Status lights:** go ONLINE on a heartbeat or `online` status. They go OFFLINE on `offline` or after `HEARTBEAT_TIMEOUT` seconds of silence.
- **Current phase:** the number from `room/state` shows as "Step N" and highlights that row of the workflow list. Text from `room/details` shows underneath.
- **Popups:** a green popup confirms a command was sent. A red popup means it wasn't, for example because the broker is down.

## Running it

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env        # then set MQTT_PASS
venv/bin/python app.py
```

Open `http://<pi-ip>:5000`.

To run it on boot:

```bash
sudo cp gm-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gm-server
journalctl -u gm-server -f          # view logs
```

## Testing without hardware

`fake_devices.py` pretends to be every room's controller and PLC. It sends heartbeats, prints each command it receives, and moves the room one workflow step forward per command.

```bash
SIM_PASS='<broker password>' venv/bin/python fake_devices.py      # all rooms
SIM_PASS='<broker password>' venv/bin/python fake_devices.py st   # one room
```

Press Ctrl+C to stop it. The page's lights should go red within `HEARTBEAT_TIMEOUT` seconds.

## Requirements

- Python 3.9+
- Flask, Flask-SocketIO, paho-mqtt (see `requirements.txt`)
- A Mosquitto broker with a user allowed to write `rr/+/+/cmd` and read `state`, `status`, `heartbeat`, and `details`
