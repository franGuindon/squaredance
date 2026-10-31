import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import call, patch

import app


class AppTests(unittest.TestCase):
    def test_parse_properties(self):
        properties = app.parse_properties(
            "Controller 00:11:22:33:44:55 (public)\n"
            "\tName: test-host\n"
            "\tPowered: yes\n"
            "\tDiscovering: no\n"
        )
        self.assertEqual(properties["Name"], "test-host")
        self.assertEqual(properties["Powered"], "yes")

    def test_get_devices_identifies_a2dp_role_from_advertised_uuids(self):
        output = (
            "Device 11:22:33:44:55:66 JTA-245\n"
            "Device 22:33:44:55:66:77 phone\n"
            "Device 33:44:55:66:77:88 headphones\n"
        )
        info = {
            "11:22:33:44:55:66": (
                "Device 11:22:33:44:55:66 (public)\n"
                "\tName: JTA-245\n\tPaired: yes\n\tConnected: no\n"
                "\tUUID: Audio Sink (0000110b-0000-1000-8000-00805f9b34fb)\n"
            ),
            "22:33:44:55:66:77": (
                "Device 22:33:44:55:66:77 (public)\n"
                "\tName: phone\n\tPaired: yes\n\tConnected: no\n"
                "\tUUID: Audio Source (0000110a-0000-1000-8000-00805f9b34fb)\n"
            ),
            "33:44:55:66:77:88": (
                "Device 33:44:55:66:77:88 (public)\n"
                "\tName: headphones\n\tPaired: yes\n\tConnected: no\n"
                "\tUUID: Audio Sink (0000110b-0000-1000-8000-00805f9b34fb)\n"
                "\tUUID: Audio Source (0000110a-0000-1000-8000-00805f9b34fb)\n"
            ),
        }
        with patch(
            "app.bluetooth",
            side_effect=lambda *args: output if args == ("devices", "Paired") else info[args[1]],
        ):
            devices = app.get_devices()
        self.assertEqual(
            [device["audio_role"] for device in devices],
            ["sink", "source", "bidirectional"],
        )

    def test_get_hci_adapter_matches_active_bluez_address(self):
        with patch(
            "app.bluetooth",
            return_value="Controller 78:2B:46:D3:0D:8C (public)\n",
        ):
            with patch(
                "app.run_command",
                return_value=app.subprocess.CompletedProcess(
                    ["hciconfig", "-a"],
                    0,
                    "hci0: Type: Primary  Bus: USB\n"
                    "\tBD Address: 78:2B:46:D3:0C\n"
                    "\tClass: 0x7c010c\n"
                    "hci1: Type: Primary  Bus: USB\n"
                    "\tBD Address: 78:2B:46:D3:0D:8C\n"
                    "\tClass: 0x7c010c\n",
                    "",
                ),
            ):
                self.assertEqual(app.get_hci_adapter(), ("hci1", 0x7C010C))

    def test_change_hci_class_uses_fixed_privileged_command_and_verifies(self):
        with patch("app.run_command") as run_command:
            run_command.side_effect = [
                app.subprocess.CompletedProcess([], 0, "", ""),
                app.subprocess.CompletedProcess(
                    [], 0, "hci0: Class: 0x240414\n", ""
                ),
            ]
            app.change_hci_class("hci0", app.SPEAKER_DEVICE_CLASS)
        run_command.assert_has_calls(
            [
                call(
                    [
                        "pkexec",
                        "/usr/bin/hciconfig",
                        "hci0",
                        "class",
                        "0x240414",
                    ],
                    timeout=60,
                ),
                call(["hciconfig", "hci0", "class"]),
            ]
        )

    def test_enable_and_restore_speaker_device_class(self):
        with patch("app.get_hci_adapter", return_value=("hci0", 0x7C010C)):
            with patch("app.change_hci_class") as change:
                with patch.object(app, "ORIGINAL_DEVICE_CLASS", None):
                    app.enable_speaker_device_class()
                    self.assertEqual(
                        app.ORIGINAL_DEVICE_CLASS, ("hci0", 0x7C010C)
                    )
                    app.restore_device_class()
                    self.assertIsNone(app.ORIGINAL_DEVICE_CLASS)
        self.assertEqual(
            change.call_args_list,
            [
                call("hci0", app.SPEAKER_DEVICE_CLASS),
                call("hci0", 0x7C010C),
            ],
        )

    def test_failed_speaker_class_change_keeps_original_class(self):
        with patch("app.get_hci_adapter", return_value=("hci0", 0x7C010C)):
            with patch(
                "app.change_hci_class",
                side_effect=app.AppError("authorization denied"),
            ):
                with patch.object(app, "ORIGINAL_DEVICE_CLASS", None):
                    with self.assertRaisesRegex(
                        app.AppError, "authorization denied"
                    ):
                        app.enable_speaker_device_class()
                    self.assertIsNone(app.ORIGINAL_DEVICE_CLASS)

    def test_enable_speaker_config_creates_drop_in_once(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.object(Path, "home", return_value=Path(home)):
                self.assertTrue(app.enable_speaker_config())
                path = Path(home) / app.CONFIG_RELATIVE_PATH
                self.assertEqual(path.read_text(encoding="utf-8"), app.CONFIG_CONTENT)
                self.assertFalse(app.enable_speaker_config())

    def test_enable_speaker_config_does_not_overwrite_user_file(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.object(Path, "home", return_value=Path(home)):
                path = Path(home) / app.CONFIG_RELATIVE_PATH
                path.parent.mkdir(parents=True)
                path.write_text("-- user config\n", encoding="utf-8")
                with self.assertRaises(app.AppError):
                    app.enable_speaker_config()
                self.assertEqual(path.read_text(encoding="utf-8"), "-- user config\n")

    def test_failed_wireplumber_restart_removes_new_config(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.object(Path, "home", return_value=Path(home)):
                with patch("app.run_command", side_effect=app.AppError("restart failed")):
                    with self.assertRaisesRegex(app.AppError, "restart failed"):
                        app.start_speaker_mode()
                self.assertFalse((Path(home) / app.CONFIG_RELATIVE_PATH).exists())

    def test_speaker_mode_stays_discoverable_and_registers_agent(self):
        with patch("app.enable_speaker_config", return_value=False):
            with patch("app.enable_speaker_device_class"):
                with patch("app.get_hci_adapter", return_value=("hci0", 0x7C010C)):
                    with patch("app.start_pairing_agent") as start_agent:
                        with patch("app.bluetooth", return_value="") as bluetooth:
                            with patch(
                                "app.get_status",
                                side_effect=lambda: {
                                    "pairing_mode": app.PAIRING_WINDOW_ACTIVE
                                },
                            ):
                                with patch.object(app, "PAIRING_AGENT", None):
                                    with patch.object(
                                        app, "PAIRING_WINDOW_ACTIVE", False
                                    ):
                                        status = app.start_speaker_mode()
        self.assertEqual(status, {"pairing_mode": True})
        start_agent.assert_called_once_with()
        self.assertEqual(
            bluetooth.call_args_list,
            [
                call("power", "on"),
                call("discoverable-timeout", "0"),
                call("pairable", "on"),
                call("discoverable", "on"),
            ],
        )

    def test_failed_start_closes_discovery_and_unregisters_agent(self):
        with patch("app.enable_speaker_config", return_value=False):
            with patch("app.enable_speaker_device_class"):
                with patch("app.get_hci_adapter", return_value=("hci0", 0x7C010C)):
                    with patch("app.restore_device_class") as restore_class:
                        with patch("app.start_pairing_agent"):
                            with patch("app.stop_pairing_agent") as stop_agent:
                                with patch(
                                    "app.bluetooth",
                                    side_effect=[
                                        "",
                                        "",
                                        app.AppError("pairable failed"),
                                        "",
                                        "",
                                    ],
                                ) as bluetooth:
                                    with patch.object(app, "PAIRING_AGENT", None):
                                        with patch.object(
                                            app, "PAIRING_WINDOW_ACTIVE", False
                                        ):
                                            with self.assertRaisesRegex(
                                                app.AppError, "pairable failed"
                                            ):
                                                app.start_speaker_mode()
        self.assertEqual(
            bluetooth.call_args_list[-2:],
            [call("discoverable", "off"), call("pairable", "off")],
        )
        stop_agent.assert_called_once_with()
        restore_class.assert_called_once_with()

    def test_stopping_speaker_mode_disables_discovery_and_agent(self):
        with patch("app.bluetooth", return_value="") as bluetooth:
            with patch("app.stop_pairing_agent") as stop_agent:
                with patch("app.restore_device_class") as restore_class:
                    with patch(
                        "app.get_status", return_value={"pairing_mode": False}
                    ):
                        with patch.object(app, "PAIRING_WINDOW_ACTIVE", True):
                            status = app.stop_discoverability()
        self.assertEqual(status, {"pairing_mode": False})
        self.assertEqual(
            bluetooth.call_args_list,
            [call("discoverable", "off"), call("pairable", "off")],
        )
        stop_agent.assert_called_once_with()
        restore_class.assert_called_once_with()

    def test_connect_device_trusts_then_connects_paired_device(self):
        address = "12:34:56:78:9A:BC"
        with patch(
            "app.get_devices",
            return_value=[{"address": address, "name": "phone", "connected": False}],
        ):
            with patch("app.bluetooth") as bluetooth:
                with patch("app.run_command") as run_command:
                    status = {
                        "devices": [
                            {"address": address, "name": "phone", "connected": True}
                        ]
                    }
                    with patch("app.get_status", return_value=status):
                        self.assertEqual(
                            app.connect_device(address), status
                        )
        bluetooth.assert_called_once_with("trust", address)
        run_command.assert_called_once_with(
            ["bluetoothctl", "--timeout", "30", "connect", address], timeout=35
        )

    def test_connect_device_requires_a_paired_device(self):
        with patch("app.get_devices", return_value=[]):
            with self.assertRaisesRegex(app.AppError, "Pair this device first"):
                app.connect_device("12:34:56:78:9A:BC")
        with self.assertRaisesRegex(app.AppError, "Invalid Bluetooth device"):
            app.connect_device("not-an-address")

    def test_connect_device_rejects_receiver_only_turntable_mode(self):
        address = "12:34:56:78:9A:BC"
        with patch(
            "app.get_devices",
            return_value=[
                {
                    "address": address,
                    "name": "JTA-245",
                    "connected": False,
                    "audio_role": "sink",
                }
            ],
        ):
            with self.assertRaisesRegex(app.AppError, "Bluetooth transmit/source mode"):
                app.connect_device(address)

    def test_connect_device_reports_missing_connection(self):
        address = "12:34:56:78:9A:BC"
        with patch(
            "app.get_devices",
            return_value=[{"address": address, "name": "phone", "connected": False}],
        ):
            with patch("app.bluetooth"):
                with patch("app.run_command"):
                    with patch(
                        "app.get_status",
                        return_value={
                            "devices": [
                                {
                                    "address": address,
                                    "name": "phone",
                                    "connected": False,
                                }
                            ]
                        },
                    ):
                        with self.assertRaisesRegex(
                            app.AppError, "did not establish a connection"
                        ):
                            app.connect_device(address)

    def test_start_recording_requires_connected_audio_device(self):
        with patch(
            "app.get_bluetooth_capture_source",
            side_effect=app.AppError("Connect a Bluetooth audio source"),
        ):
            with self.assertRaisesRegex(app.AppError, "Connect a Bluetooth audio source"):
                app.start_recording()

    def test_start_recording_targets_default_output_and_creates_wav_path(self):
        with tempfile.TemporaryDirectory() as directory:
            process = unittest.mock.Mock()
            process.poll.return_value = None
            with patch("app.recordings_directory", return_value=Path(directory)):
                with patch(
                    "app.get_bluetooth_capture_source",
                    return_value="bluez_input.jta.2",
                ):
                    with patch("app.link_recording_to_bluetooth_stream") as link_stream:
                        with patch("app.subprocess.Popen", return_value=process) as popen:
                            with patch("app.time.sleep"):
                                with patch("app.get_status", return_value={"recording": {}}):
                                    with patch.object(app, "RECORDING_PROCESS", None):
                                        with patch.object(app, "RECORDING_PATH", None):
                                            with patch.object(app, "LAST_RECORDING_PATH", None):
                                                with patch.object(app, "RECORDING_ERROR", None):
                                                    status = app.start_recording()
            command = popen.call_args.args[0]
            self.assertEqual(command[:7], [
                "pw-record", "--target", "0", "--rate", "48000",
                "--channels", "2",
            ])
            self.assertEqual(command[7:9], ["--format", "s16"])
            self.assertIn("--properties", command)
            self.assertTrue(command[-1].startswith(directory))
            self.assertTrue(command[-1].endswith(".wav"))
            link_stream.assert_called_once()
            self.assertEqual(status, {"recording": {}})

    def test_bluetooth_capture_source_uses_only_incoming_a2dp_stream(self):
        address = "45:B0:83:D1:9A:CF"
        stream_name = "bluez_input.45_B0_83_D1_9A_CF.2"
        listing = (
            " id 33, type PipeWire:Interface:Node/3\n"
            f'  node.name = "{stream_name}"\n'
        )
        info = (
            "id: 33\n* properties:\n"
            f'*\tapi.bluez5.address = "{address}"\n'
            '*\tapi.bluez5.profile = "a2dp-source"\n'
            '*\tmedia.class = "Stream/Output/Audio"\n'
            f'*\tnode.name = "{stream_name}"\n'
        )
        with patch(
            "app.get_devices",
            return_value=[
                {
                    "address": address,
                    "name": "JTA-245",
                    "connected": True,
                    "audio_role": "bidirectional",
                }
            ],
        ):
            with patch(
                "app.run_command",
                side_effect=[
                    app.subprocess.CompletedProcess([], 0, listing, ""),
                    app.subprocess.CompletedProcess([], 0, info, ""),
                    app.subprocess.CompletedProcess(
                        [],
                        0,
                        f"{stream_name}:output_FL\n{stream_name}:output_FR\n",
                        "",
                    ),
                ],
            ) as run_command:
                self.assertEqual(app.get_bluetooth_capture_source(), stream_name)
        self.assertEqual(
            [call.args[0] for call in run_command.call_args_list],
            [
                ["pw-cli", "ls", "Node"],
                ["pw-cli", "info", "33"],
                ["pw-link", "-o"],
            ],
        )

    def test_recording_links_directly_to_bluetooth_not_default_input(self):
        source = "bluez_input.45_B0_83_D1_9A_CF.2"
        recorder = "computer_speaker_recorder_123"
        links = (
            f"{source}:output_FL\n  |-> {recorder}:input_FL\n"
            f"{source}:output_FR\n  |-> {recorder}:input_FR\n"
            f"{recorder}:input_FL\n  |<- {source}:output_FL\n"
            f"{recorder}:input_FR\n  |<- {source}:output_FR\n"
        )
        process = unittest.mock.Mock()
        process.poll.return_value = None
        with patch(
            "app.run_command",
            side_effect=[
                app.subprocess.CompletedProcess(
                    [], 0, f"{recorder}:input_FL\n{recorder}:input_FR\n", ""
                ),
                app.subprocess.CompletedProcess([], 0, "", ""),
                app.subprocess.CompletedProcess([], 0, "", ""),
                app.subprocess.CompletedProcess([], 0, links, ""),
            ],
        ) as run_command:
            with patch("app.time.sleep"):
                app.link_recording_to_bluetooth_stream(source, recorder, process)
        self.assertEqual(
            [call.args[0] for call in run_command.call_args_list],
            [
                ["pw-link", "-i"],
                ["pw-link", f"{source}:output_FL", f"{recorder}:input_FL"],
                ["pw-link", f"{source}:output_FR", f"{recorder}:input_FR"],
                ["pw-link", "-l"],
            ],
        )

    def test_stop_recording_sends_interrupt_and_verifies_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.wav"
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(48000)
                wav_file.writeframes(b"\0" * 8)
            process = unittest.mock.Mock()
            process.poll.return_value = None
            process.returncode = -app.signal.SIGINT
            process.communicate.return_value = (None, b"")
            with patch.object(app, "RECORDING_PROCESS", process):
                with patch.object(app, "RECORDING_PATH", path):
                    with patch.object(app, "LAST_RECORDING_PATH", None):
                        with patch.object(app, "RECORDING_ERROR", None):
                            with patch("app.get_status", return_value={"recording": {}}):
                                status = app.stop_recording()
            process.send_signal.assert_called_once_with(app.signal.SIGINT)
            process.communicate.assert_called_once_with(timeout=10)
            self.assertEqual(status, {"recording": {}})


if __name__ == "__main__":
    unittest.main()
