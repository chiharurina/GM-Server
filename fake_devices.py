"""
Pretend to be the ESP32s and PLC so you can test the GM page with no hardware.

  python fake_devices.py            all rooms online
  python fake_devices.py st         only the Haunted House room

Every command the GM page sends is printed and moves the room one step forward.
Ctrl+C stops it, and the devices should go OFFLINE on the page within 3 seconds.

Uses the nodered login (it's allowed to publish heartbeats). Set it with
  export SIM_PASS='your-nodered-password'
"""
import os
import sys
import time

import paho.mqtt.client as mqtt

HOST = os.environ.get('MQTT_HOST', 'localhost')
USER = os.environ.get('SIM_USER', 'nodered')
PASS = os.environ.get('SIM_PASS', '')

DEVICES = {
    "mr":   ["ronnie", "plc"],
    "tron": ["controller", "plc"],
    "ij":   ["winston", "plc"],
    "st":   ["demogorgon", "plc"],
}
rooms = sys.argv[1:] or list(DEVICES)
step = {r: 1 for r in rooms}


def on_connect(c, u, f, rc, p=None):
    if rc != 0:
        print("Connect refused - check SIM_PASS")
        return
    print("Simulator connected. Rooms:", ", ".join(rooms))
    for r in rooms:
        c.subscribe(f"rr/{r}/+/cmd", 1)
        for d in DEVICES[r]:
            c.publish(f"rr/{r}/{d}/status", "online", qos=1, retain=True)
        c.publish(f"rr/{r}/room/state", str(step[r]), qos=1, retain=True)


def on_message(c, u, msg):
    r = msg.topic.split('/')[1]
    cmd = msg.payload.decode()
    print(f"GOT  {msg.topic}  {cmd}")
    step[r] = 1 if cmd == "reset" and msg.topic.endswith("/room/cmd") else step[r] + 1
    c.publish(f"rr/{r}/room/state", str(step[r]), qos=1, retain=True)
    c.publish(f"rr/{r}/room/details", f"Last command: {cmd}", qos=1, retain=True)


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rr-simulator")
c.username_pw_set(USER, PASS)
c.on_connect = on_connect
c.on_message = on_message
c.connect(HOST, 1883, 10)
c.loop_start()

try:
    while True:
        for r in rooms:
            for d in DEVICES[r]:
                c.publish(f"rr/{r}/{d}/heartbeat", "1")
        time.sleep(1)
except KeyboardInterrupt:
    # Simulate devices dying: no clean "offline", let the heartbeat watchdog catch it
    print("\nStopped. Devices should go OFFLINE on the page within 3 s.")
    c.loop_stop()
