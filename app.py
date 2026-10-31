#!/usr/bin/env python3
"""Local web app for using a Linux computer as a Bluetooth speaker."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


APP_DIR = Path(__file__).resolve().parent
SPEAKER_DEVICE_CLASS = 0x240414
CONFIG_RELATIVE_PATH = Path(
    ".config/wireplumber/bluetooth.lua.d/51-computer-speaker.lua"
)
CONFIG_CONTENT = """\
bluez_monitor.properties["bluez5.roles"] = "[ a2dp_sink a2dp_source bap_sink bap_source hfp_hf hfp_ag ]"
"""
CONFIG_LOCK = threading.Lock()
PAIRING_LOCK = threading.RLock()
PAIRING_AGENT: Any | None = None
PAIRING_WINDOW_ACTIVE = False
ORIGINAL_DEVICE_CLASS: tuple[str, int] | None = None
RECORDING_LOCK = threading.RLock()
RECORDING_PROCESS: subprocess.Popen[bytes] | None = None
RECORDING_PATH: Path | None = None
LAST_RECORDING_PATH: Path | None = None
RECORDING_ERROR: str | None = None


class AppError(Exception):
    """An expected failure while controlling the system Bluetooth/audio stack."""


def run_command(
    command: list[str], *, timeout: int = 12, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    if shutil.which(command[0]) is None:
        raise AppError(f"Required system command not found: {command[0]}")
    try:
        result = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(f"{command[0]} did not respond in time.") from exc
    except OSError as exc:
        raise AppError(f"Could not run {command[0]}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise AppError(detail or f"{command[0]} exited with status {result.returncode}.")
    return result


def bluetooth(*arguments: str) -> str:
    return run_command(["bluetoothctl", *arguments]).stdout


def get_hci_adapter() -> tuple[str, int]:
    controller_lines = bluetooth("show").splitlines()
    if not controller_lines:
        raise AppError("BlueZ returned no active Bluetooth controller.")
    controller = controller_lines[0]
    address_match = re.search(r"Controller\s+([0-9A-Fa-f:]{17})", controller)
    if not address_match:
        raise AppError("Could not identify the active Bluetooth adapter.")
    address = address_match.group(1).lower()
    adapters = run_command(["hciconfig", "-a"]).stdout
    for block in re.split(r"(?=^[a-zA-Z0-9]+:\s+Type:)", adapters, flags=re.MULTILINE):
        name_match = re.match(r"^([a-zA-Z0-9]+):\s+Type:", block)
        address_match = re.search(r"BD Address:\s*([0-9A-Fa-f:]{17})", block)
        class_match = re.search(r"Class:\s*(0x[0-9A-Fa-f]{6})", block)
        if (
            name_match
            and address_match
            and address_match.group(1).lower() == address
            and class_match
        ):
            return name_match.group(1), int(class_match.group(1), 16)
    raise AppError(
        "Could not find the active adapter in hciconfig output. "
        "Install the BlueZ compatibility tools and retry."
    )


def change_hci_class(adapter: str, device_class: int) -> None:
    try:
        run_command(
            [
                "pkexec",
                "/usr/bin/hciconfig",
                adapter,
                "class",
                f"0x{device_class:06x}",
            ],
            timeout=60,
        )
    except AppError as exc:
        raise AppError(
            "Could not change the Bluetooth device class. Allow the administrator "
            "authentication prompt when it appears; this is needed so the JTA-245 "
            "recognizes the laptop as a speaker. "
            f"Details: {exc}"
        ) from exc
    reported = run_command(["hciconfig", adapter, "class"]).stdout
    match = re.search(r"Class:\s*(0x[0-9A-Fa-f]{6})", reported)
    if not match or int(match.group(1), 16) != device_class:
        actual = match.group(1) if match else "unknown"
        raise AppError(
            f"The Bluetooth adapter still reports device class {actual} after "
            f"requesting 0x{device_class:06x}."
        )


def enable_speaker_device_class() -> None:
    global ORIGINAL_DEVICE_CLASS
    adapter, current_class = get_hci_adapter()
    if current_class == SPEAKER_DEVICE_CLASS:
        return
    if ORIGINAL_DEVICE_CLASS is None:
        ORIGINAL_DEVICE_CLASS = (adapter, current_class)
    try:
        change_hci_class(adapter, SPEAKER_DEVICE_CLASS)
    except AppError as change_error:
        try:
            _, actual_class = get_hci_adapter()
            if actual_class == current_class:
                ORIGINAL_DEVICE_CLASS = None
            else:
                restore_device_class()
        except AppError as restore_error:
            raise AppError(
                f"Could not set the speaker device class ({change_error}) and "
                f"could not confirm or restore the original class ({restore_error})."
            ) from change_error
        raise


def restore_device_class() -> None:
    global ORIGINAL_DEVICE_CLASS
    original = ORIGINAL_DEVICE_CLASS
    if original is None:
        return
    adapter, device_class = original
    change_hci_class(adapter, device_class)
    ORIGINAL_DEVICE_CLASS = None


def speaker_config_path() -> Path:
    return Path.home() / CONFIG_RELATIVE_PATH


def start_pairing_agent() -> Any:
    global PAIRING_AGENT
    if PAIRING_AGENT is not None:
        return PAIRING_AGENT
    try:
        from bluetooth_agent import create_pairing_agent
    except ImportError as exc:
        raise AppError(
            "Automatic pairing requires the system Python D-Bus packages. "
            "Install python3-dbus and python3-gi, then run the app with system python3."
        ) from exc
    try:
        PAIRING_AGENT = create_pairing_agent()
    except RuntimeError as exc:
        raise AppError(str(exc)) from exc
    return PAIRING_AGENT


def enable_speaker_config() -> bool:
    """Install the WirePlumber 0.4 drop-in; return whether it was newly created."""
    path = speaker_config_path()
    with CONFIG_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppError(f"Could not create WirePlumber config directory: {exc}") from exc
        try:
            with path.open("x", encoding="utf-8") as config:
                config.write(CONFIG_CONTENT)
            return True
        except FileExistsError:
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise AppError(f"Could not read WirePlumber config {path}: {exc}") from exc
            if existing != CONFIG_CONTENT:
                raise AppError(
                    f"Refusing to overwrite your existing WirePlumber config: {path}"
                )
            return False
        except OSError as exc:
            raise AppError(f"Could not write WirePlumber config {path}: {exc}") from exc


def parse_properties(output: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"\s*([^:]+):\s*(.*?)\s*$", line)
        if match:
            properties[match.group(1)] = match.group(2)
    return properties


def get_devices() -> list[dict[str, Any]]:
    output = bluetooth("devices", "Paired")
    devices: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.+)", line)
        if not match:
            continue
        address, name = match.groups()
        info_output = bluetooth("info", address)
        info = parse_properties(info_output)
        uuids = re.findall(
            r"^\s*UUID:.*\(([0-9A-Fa-f-]{36})\)\s*$",
            info_output,
            flags=re.MULTILINE,
        )
        has_audio_source = any(
            uuid.lower() == "0000110a-0000-1000-8000-00805f9b34fb"
            for uuid in uuids
        )
        has_audio_sink = any(
            uuid.lower() == "0000110b-0000-1000-8000-00805f9b34fb"
            for uuid in uuids
        )
        audio_role = (
            "bidirectional"
            if has_audio_source and has_audio_sink
            else "source"
            if has_audio_source
            else "sink"
            if has_audio_sink
            else None
        )
        devices.append(
            {
                "address": address,
                "name": info.get("Alias", info.get("Name", name)),
                "connected": info.get("Connected", "no").lower() == "yes",
                "audio_role": audio_role,
            }
        )
    return devices


def connect_device(address: str) -> dict[str, Any]:
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address):
        raise AppError("Invalid Bluetooth device address.")
    devices = get_devices()
    device = next(
        (item for item in devices if item["address"].lower() == address.lower()),
        None,
    )
    if device is None:
        raise AppError("Pair this device first, then connect it from this app.")
    if device.get("audio_role") == "sink":
        raise AppError(
            f"{device['name']} is advertising an A2DP receiver profile. To send "
            "audio to this computer, switch it to Bluetooth transmit/source mode."
        )
    bluetooth("trust", address)
    run_command(["bluetoothctl", "--timeout", "30", "connect", address], timeout=35)
    status = get_status()
    connected = next(
        (
            item
            for item in status["devices"]
            if item["address"].lower() == address.lower()
        ),
        None,
    )
    if connected is None or not connected["connected"]:
        raise AppError(
            "Bluetooth did not establish a connection. Confirm the phone supports "
            "Bluetooth audio playback to a computer, then try again."
        )
    return status


def get_audio_outputs() -> list[str]:
    output = run_command(["wpctl", "status"]).stdout
    outputs: list[str] = []
    in_sinks = False
    for line in output.splitlines():
        if re.search(r"(?:├─|└─)\s*Sinks:", line):
            in_sinks = True
            continue
        if in_sinks and re.search(
            r"(?:├─|└─)\s*(?:Sink endpoints|Sources|Source endpoints|Streams|Devices|Video|Settings):",
            line,
        ):
            in_sinks = False
        if in_sinks:
            match = re.search(
                r"\*?\s*\d+\.\s+(.+?)\s*(?:\[vol:.*)?$",
                line.strip(" │├└─"),
            )
            if match:
                outputs.append(match.group(1).strip())
    return outputs


def recordings_directory() -> Path:
    return Path.home() / "Music" / "Computer Speaker"


def get_bluetooth_capture_source() -> str:
    devices = [
        device
        for device in get_devices()
        if device["connected"] and device.get("audio_role") != "sink"
    ]
    if not devices:
        raise AppError("Connect a Bluetooth audio source before recording.")

    nodes = run_command(["pw-cli", "ls", "Node"]).stdout
    node_blocks = re.split(
        r"(?=^\s*id\s+\d+,\s+type PipeWire:Interface:Node/3)",
        nodes,
        flags=re.MULTILINE,
    )
    capture_sources: list[str] = []
    for device in devices:
        normalized_address = device["address"].replace(":", "_")
        for block in node_blocks:
            name_match = re.search(
                r'^\s*node\.name\s*=\s*"([^"]+)"',
                block,
                flags=re.MULTILINE,
            )
            id_match = re.match(r"\s*id\s+(\d+),", block)
            if (
                not name_match
                or not id_match
                or not name_match.group(1).startswith(
                    f"bluez_input.{normalized_address}."
                )
            ):
                continue
            info = run_command(["pw-cli", "info", id_match.group(1)]).stdout
            properties = {
                key: value
                for key, value in re.findall(
                    r'^\*\s+([a-zA-Z0-9_.-]+)\s*=\s*"([^"]*)"\s*$',
                    info,
                    flags=re.MULTILINE,
                )
            }
            if (
                properties.get("api.bluez5.address", "").lower()
                == device["address"].lower()
                and properties.get("api.bluez5.profile") == "a2dp-source"
                and properties.get("media.class") == "Stream/Output/Audio"
            ):
                capture_sources.append(properties["node.name"])

    if not capture_sources:
        raise AppError(
            "PipeWire has no active incoming Bluetooth audio stream to record. "
            "Make sure the turntable is connected and playing audio, then retry."
        )
    if len(capture_sources) != 1:
        raise AppError(
            "More than one Bluetooth audio stream is active. Disconnect the "
            "unwanted audio device before recording so the app can select "
            "the intended source."
        )
    source = capture_sources[0]
    output_ports = run_command(["pw-link", "-o"]).stdout.splitlines()
    for channel in ("FL", "FR"):
        if f"{source}:output_{channel}" not in output_ports:
            raise AppError(
                f"PipeWire is missing the Bluetooth {channel} audio stream port."
            )
    return source


def link_recording_to_bluetooth_stream(
    source: str, recorder: str, process: subprocess.Popen[bytes]
) -> None:
    input_ports: set[str] = set()
    for _ in range(20):
        if process.poll() is not None:
            break
        input_ports = set(run_command(["pw-link", "-i"]).stdout.splitlines())
        if all(f"{recorder}:input_{channel}" in input_ports for channel in ("FL", "FR")):
            break
        time.sleep(0.1)
    else:
        raise AppError("PipeWire did not create the recording audio ports.")

    for channel in ("FL", "FR"):
        run_command(
            [
                "pw-link",
                f"{source}:output_{channel}",
                f"{recorder}:input_{channel}",
            ]
        )
    links = run_command(["pw-link", "-l"]).stdout
    if not all(
        f"{recorder}:input_{channel}\n  |<- {source}:output_{channel}"
        in links
        for channel in ("FL", "FR")
    ):
        raise AppError("Could not verify a direct recording link from Bluetooth audio.")


def get_recording_status() -> dict[str, Any]:
    global RECORDING_PROCESS, RECORDING_PATH, LAST_RECORDING_PATH, RECORDING_ERROR
    with RECORDING_LOCK:
        process = RECORDING_PROCESS
        if process is not None and process.poll() is not None:
            _, stderr = process.communicate()
            if process.returncode:
                detail = stderr.decode(errors="replace").strip() if stderr else ""
                RECORDING_ERROR = detail or (
                    f"The recording process exited with status {process.returncode}."
                )
            LAST_RECORDING_PATH = RECORDING_PATH
            RECORDING_PROCESS = None
            RECORDING_PATH = None
        path = RECORDING_PATH or LAST_RECORDING_PATH
        return {
            "active": RECORDING_PROCESS is not None,
            "file": str(path) if path is not None else None,
            "error": RECORDING_ERROR,
        }


def start_recording() -> dict[str, Any]:
    global RECORDING_PROCESS, RECORDING_PATH, RECORDING_ERROR
    with RECORDING_LOCK:
        current = get_recording_status()
        if current["active"]:
            raise AppError("A recording is already in progress.")
        source = get_bluetooth_capture_source()
        try:
            output_directory = recordings_directory()
            output_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppError(f"Could not create the recordings folder: {exc}") from exc
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        recording_path = output_directory / f"recording_{timestamp}.wav"
        recorder_name = f"computer_speaker_recorder_{os.getpid()}"
        try:
            process = subprocess.Popen(
                [
                    "pw-record",
                    "--target",
                    "0",
                    "--rate",
                    "48000",
                    "--channels",
                    "2",
                    "--format",
                    "s16",
                    "--properties",
                    f'{{ node.name = "{recorder_name}" }}',
                    str(recording_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise AppError(f"Could not start PipeWire recording: {exc}") from exc
        RECORDING_PROCESS = process
        RECORDING_PATH = recording_path
        RECORDING_ERROR = None
        try:
            time.sleep(0.2)
            if process.poll() is not None:
                _, stderr = process.communicate()
                detail = stderr.decode(errors="replace").strip() if stderr else ""
                raise AppError(
                    "PipeWire could not start recording."
                    + (f" {detail}" if detail else "")
                )
            link_recording_to_bluetooth_stream(source, recorder_name, process)
        except AppError as exc:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.communicate(timeout=3)
            RECORDING_PROCESS = None
            RECORDING_PATH = None
            try:
                recording_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise AppError(
                    f"Recording could not start ({exc}); incomplete file "
                    f"{recording_path} could not be removed: {cleanup_error}"
                ) from exc
            raise
        except OSError as exc:
            if process.poll() is None:
                process.terminate()
            process.communicate()
            RECORDING_PROCESS = None
            RECORDING_PATH = None
            try:
                recording_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise AppError(
                    f"Could not connect the Bluetooth recording stream ({exc}); "
                    f"incomplete file {recording_path} could not be removed: "
                    f"{cleanup_error}"
                ) from exc
            raise AppError(f"Could not connect the Bluetooth recording stream: {exc}") from exc
    return get_status()


def stop_recording() -> dict[str, Any]:
    global RECORDING_PROCESS, RECORDING_PATH, LAST_RECORDING_PATH, RECORDING_ERROR
    with RECORDING_LOCK:
        process = RECORDING_PROCESS
        path = RECORDING_PATH
        if process is None or path is None:
            raise AppError("There is no recording in progress.")
        interrupted = process.poll() is None
        if interrupted:
            process.send_signal(signal.SIGINT)
        try:
            _, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            _, stderr = process.communicate(timeout=3)
            RECORDING_PROCESS = None
            RECORDING_PATH = None
            LAST_RECORDING_PATH = path
            RECORDING_ERROR = "PipeWire did not stop recording cleanly."
            raise AppError(RECORDING_ERROR)
        RECORDING_PROCESS = None
        RECORDING_PATH = None
        LAST_RECORDING_PATH = path
        if process.returncode not in (0, -signal.SIGINT) and not (
            interrupted and process.returncode == 1
        ):
            detail = stderr.decode(errors="replace").strip() if stderr else ""
            RECORDING_ERROR = detail or (
                f"The recording process exited with status {process.returncode}."
            )
            raise AppError(f"Recording stopped unexpectedly: {RECORDING_ERROR}")
        try:
            with wave.open(str(path), "rb") as wav_file:
                if wav_file.getnchannels() != 2 or wav_file.getframerate() != 48000:
                    raise AppError("The saved recording has an unexpected WAV format.")
        except (OSError, wave.Error) as exc:
            RECORDING_ERROR = f"Could not verify the saved WAV file: {exc}"
            raise AppError(RECORDING_ERROR) from exc
        RECORDING_ERROR = None
    return get_status()


def get_status() -> dict[str, Any]:
    controller = parse_properties(bluetooth("show"))
    try:
        audio_outputs = get_audio_outputs()
        audio_error = None
    except AppError as exc:
        audio_outputs = []
        audio_error = str(exc)
    try:
        _, current_device_class = get_hci_adapter()
        device_class = f"0x{current_device_class:06x}"
        speaker_class_active = current_device_class == SPEAKER_DEVICE_CLASS
        device_class_error = None
    except AppError as exc:
        device_class = None
        speaker_class_active = False
        device_class_error = str(exc)
    return {
        "adapter": {
            "name": controller.get("Alias", controller.get("Name", "Bluetooth adapter")),
            "powered": controller.get("Powered", "no").lower() == "yes",
            "discoverable": controller.get("Discoverable", "no").lower() == "yes",
            "pairable": controller.get("Pairable", "no").lower() == "yes",
        },
        "devices": get_devices(),
        "speaker_mode": speaker_config_path().is_file(),
        "device_class": device_class,
        "speaker_class_active": speaker_class_active,
        "device_class_error": device_class_error,
        "audio_outputs": audio_outputs,
        "audio_error": audio_error,
        "pairing_mode": PAIRING_WINDOW_ACTIVE,
        "recording": get_recording_status(),
    }


def start_speaker_mode() -> dict[str, Any]:
    global PAIRING_WINDOW_ACTIVE
    created = enable_speaker_config()
    if created:
        try:
            run_command(["systemctl", "--user", "restart", "wireplumber"], timeout=20)
        except AppError as restart_error:
            path = speaker_config_path()
            try:
                if path.read_text(encoding="utf-8") == CONFIG_CONTENT:
                    path.unlink()
            except OSError as cleanup_error:
                raise AppError(
                    f"WirePlumber could not be restarted ({restart_error}) and "
                    f"the new config could not be removed: {cleanup_error}"
                ) from cleanup_error
            raise
    with PAIRING_LOCK:
        newly_registered_agent = PAIRING_AGENT is None
        _, current_class = get_hci_adapter()
        newly_changed_class = (
            current_class != SPEAKER_DEVICE_CLASS and ORIGINAL_DEVICE_CLASS is None
        )
        enable_speaker_device_class()
        try:
            start_pairing_agent()
            bluetooth("power", "on")
            bluetooth("discoverable-timeout", "0")
            bluetooth("pairable", "on")
            bluetooth("discoverable", "on")
            PAIRING_WINDOW_ACTIVE = True
        except AppError as start_error:
            cleanup_errors: list[str] = []
            for command in (("discoverable", "off"), ("pairable", "off")):
                try:
                    bluetooth(*command)
                except AppError as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            if newly_registered_agent:
                try:
                    stop_pairing_agent()
                except AppError as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            if newly_changed_class:
                try:
                    restore_device_class()
                except AppError as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            if cleanup_errors:
                raise AppError(
                    f"Could not start speaker mode: {start_error}. Cleanup also "
                    f"failed: {'; '.join(cleanup_errors)}"
                ) from start_error
            raise
    return get_status()


def stop_pairing_agent() -> None:
    global PAIRING_AGENT
    agent = PAIRING_AGENT
    PAIRING_AGENT = None
    if agent is not None:
        try:
            agent.close()
        except RuntimeError as exc:
            raise AppError(f"Could not unregister the Bluetooth pairing agent: {exc}") from exc


def _disable_pairing() -> None:
    global PAIRING_WINDOW_ACTIVE
    PAIRING_WINDOW_ACTIVE = False
    failures: list[str] = []
    for command in (("discoverable", "off"), ("pairable", "off")):
        try:
            bluetooth(*command)
        except AppError as exc:
            failures.append(str(exc))
    try:
        stop_pairing_agent()
    except AppError as exc:
        failures.append(str(exc))
    try:
        restore_device_class()
    except AppError as exc:
        failures.append(str(exc))
    if failures:
        raise AppError("Could not fully stop pairing: " + " ".join(failures))


def stop_discoverability() -> dict[str, Any]:
    with PAIRING_LOCK:
        _disable_pairing()
    return get_status()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ComputerSpeaker/1.0"

    def _is_same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin.rstrip("/") == (
            f"http://{self.headers.get('Host', '')}"
        )

    def _json(self, data: dict[str, Any], status: int = 200) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _static_file(self, name: str, content_type: str) -> None:
        path = APP_DIR / "static" / name
        try:
            content = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._static_file("index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self._static_file("style.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._static_file("app.js", "text/javascript; charset=utf-8")
        elif path == "/api/status":
            try:
                self._json(get_status())
            except AppError as exc:
                self._json({"error": str(exc)}, 503)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._is_same_origin():
            self._json({"error": "Cross-origin requests are not allowed."}, 403)
            return
        try:
            if path == "/api/speaker/start":
                self._json(start_speaker_mode())
            elif path == "/api/speaker/stop":
                self._json(stop_discoverability())
            elif path == "/api/recording/start":
                self._json(start_recording())
            elif path == "/api/recording/stop":
                self._json(stop_recording())
            elif path.startswith("/api/device/") and path.endswith("/connect"):
                address = path.removeprefix("/api/device/").removesuffix("/connect")
                self._json(connect_device(address))
            else:
                self._json({"error": "Not found."}, 404)
        except AppError as exc:
            self._json({"error": str(exc)}, 500)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Local bind address")
    parser.add_argument("--port", type=int, default=8765, help="Local web port")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("For safety, the app can only listen on the local computer.")
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(f"Computer Speaker is ready at http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Computer Speaker.")
    finally:
        server.server_close()
        if PAIRING_WINDOW_ACTIVE:
            try:
                stop_discoverability()
            except AppError as exc:
                print(f"Could not close the Bluetooth pairing window: {exc}")
        if RECORDING_PROCESS is not None:
            try:
                stop_recording()
            except AppError as exc:
                print(f"Could not finalize the WAV recording: {exc}")


if __name__ == "__main__":
    main()
