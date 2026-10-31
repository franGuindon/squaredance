# Computer Speaker

A local Linux app that lets a phone or other Bluetooth audio source play through
this computer's current audio output.

## Requirements

- BlueZ (`bluetoothctl`)
- PipeWire with WirePlumber 0.4
- `wpctl` and `systemctl`
- Python system D-Bus bindings (`python3-dbus` and `python3-gi`)
- Python 3.10 or newer

On Ubuntu, install the requirements with:

```sh
sudo apt install bluez pipewire wireplumber python3-dbus python3-gi
```

## Run

From this directory:

```sh
python3 app.py
```

Open <http://127.0.0.1:8765>, then choose **Set up speaker mode**. The app adds
a user-level WirePlumber drop-in to enable the A2DP sink role and restarts
WirePlumber. It registers a BlueZ `NoInputNoOutput` pairing agent, then keeps
Bluetooth discoverable and pairable until speaker mode is stopped. This lets
record players and other headless audio sources pair without a PIN dialog or
confirmation screen using standard Bluetooth Secure Simple Pairing and connect
using A2DP. Newly paired devices are marked trusted so BlueZ can complete the
audio connection and reconnect later. The agent automatically authorizes
audio/AVRCP profiles and rejects unrelated Bluetooth services. Phones can pair
from their Bluetooth settings; **Connect** beside a paired phone starts its
audio connection. Audio plays through the current default computer output.

Speaker mode also changes the Bluetooth Class of Device to the standard
Loudspeaker class (`0x240414`). This matters for transmitters such as the Jensen
JTA-245 that filter scan results by speaker device class instead of listing
computers. The first time speaker mode starts, approve the administrator
authentication prompt so the app can set the adapter class. The original class
is restored when speaker mode is stopped or the app exits.

For the JTA-245, select its Bluetooth transmit/TX mode to send turntable audio
to the computer. If it is advertising only the A2DP Audio Sink profile, it is
in Bluetooth receive mode; it cannot send its audio to the laptop in that mode.
The app marks that profile direction in the paired-device list.

When a Bluetooth audio device is connected, use **Start recording** and
**Stop recording** to save the computer's current audio output as a stereo,
48 kHz WAV file. The app links the recorder directly to the incoming Bluetooth
A2DP stream, so the laptop microphone and other computer playback are not
included. Recordings are saved in `~/Music/Computer Speaker`.

Speaker mode intentionally leaves the computer visible and able to accept
nearby audio-device pairing for as long as it is active. Choose **Stop speaker
mode** to turn off discoverability, pairability, and the automatic pairing
agent. It does not disconnect devices that are already connected. The app only
listens on the local computer; closing it also disables the pairing mode it
opened.
