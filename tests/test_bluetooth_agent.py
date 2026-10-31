import unittest
from unittest.mock import Mock, patch

import dbus
from bluetooth_agent import (
    ALLOWED_AUDIO_SERVICES,
    BluezPairingAgent,
    PairingRejected,
)


class BluetoothAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = object.__new__(BluezPairingAgent)

    def test_audio_and_media_control_services_are_authorized(self):
        for service in ALLOWED_AUDIO_SERVICES:
            with self.subTest(service=service):
                self.agent.AuthorizeService("/org/bluez/device", service)

    def test_unrelated_service_is_rejected(self):
        with self.assertRaises(PairingRejected):
            self.agent.AuthorizeService(
                "/org/bluez/device", "00001101-0000-1000-8000-00805f9b34fb"
            )

    def test_numeric_confirmation_is_accepted_without_input(self):
        self.agent.RequestConfirmation("/org/bluez/device", 123456)

    def test_legacy_pin_and_passkey_entry_are_rejected(self):
        with self.assertRaises(PairingRejected):
            self.agent.RequestPinCode("/org/bluez/device")
        with self.assertRaises(PairingRejected):
            self.agent.RequestPasskey("/org/bluez/device")

    def test_newly_paired_device_is_marked_trusted(self):
        self.agent.bus = Mock()
        properties = Mock()
        with patch("bluetooth_agent.dbus.Interface", return_value=properties):
            self.agent._on_device_properties_changed(
                "org.bluez.Device1",
                {"Paired": dbus.Boolean(True)},
                [],
                "/org/bluez/hci0/dev_12_34_56_78_9A_BC",
            )
        self.agent.bus.get_object.assert_called_once_with(
            "org.bluez", "/org/bluez/hci0/dev_12_34_56_78_9A_BC"
        )
        properties.Set.assert_called_once_with(
            "org.bluez.Device1", "Trusted", dbus.Boolean(True)
        )

    def test_unpaired_device_is_not_trusted(self):
        self.agent.bus = Mock()
        self.agent._on_device_properties_changed(
            "org.bluez.Device1", {"Connected": dbus.Boolean(True)}, [], "/device"
        )
        self.agent.bus.get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
