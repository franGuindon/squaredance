"""BlueZ pairing agent for audio-only, no-input Bluetooth devices."""

from __future__ import annotations

import logging
import threading

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib


BLUEZ_SERVICE = "org.bluez"
AGENT_MANAGER_PATH = "/org/bluez"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
AGENT_PATH = "/com/roomtone/ComputerSpeaker/agent"
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_CAPABILITY = "NoInputNoOutput"
ALLOWED_AUDIO_SERVICES = {
    "0000110a-0000-1000-8000-00805f9b34fb",  # A2DP Audio Source
    "0000110b-0000-1000-8000-00805f9b34fb",  # A2DP Audio Sink
    "0000110c-0000-1000-8000-00805f9b34fb",  # AVRCP Controller
    "0000110e-0000-1000-8000-00805f9b34fb",  # AVRCP Target
}


class PairingRejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class BluezPairingAgent(dbus.service.Object):
    """Automatically accept Bluetooth pairing without a confirmation UI."""

    def __init__(self) -> None:
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        super().__init__(self.bus, AGENT_PATH)
        self.loop = GLib.MainLoop()
        self.loop_thread = threading.Thread(
            target=self.loop.run, name="bluez-pairing-agent", daemon=True
        )
        self.loop_thread.start()
        self.paired_signal = self.bus.add_signal_receiver(
            self._on_device_properties_changed,
            signal_name="PropertiesChanged",
            dbus_interface="org.freedesktop.DBus.Properties",
            arg0="org.bluez.Device1",
            path_keyword="path",
        )
        try:
            manager_object = self.bus.get_object(BLUEZ_SERVICE, AGENT_MANAGER_PATH)
            self.manager = dbus.Interface(manager_object, AGENT_MANAGER_INTERFACE)
            self.manager.RegisterAgent(AGENT_PATH, AGENT_CAPABILITY)
            try:
                self.manager.RequestDefaultAgent(AGENT_PATH)
            except dbus.DBusException:
                self.manager.UnregisterAgent(AGENT_PATH)
                raise
        except dbus.DBusException:
            self.paired_signal.remove()
            self.loop.quit()
            self.loop_thread.join(timeout=2)
            self.remove_from_connection()
            raise

    def _on_device_properties_changed(
        self,
        interface: str,
        changed: dbus.Dictionary,
        invalidated: dbus.Array,
        path: str,
    ) -> None:
        if interface != "org.bluez.Device1" or not bool(changed.get("Paired", False)):
            return
        try:
            device = self.bus.get_object(BLUEZ_SERVICE, path)
            properties = dbus.Interface(device, "org.freedesktop.DBus.Properties")
            properties.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
        except dbus.DBusException:
            logging.exception("Could not trust newly paired Bluetooth audio device %s", path)

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device: str) -> str:
        raise PairingRejected("Legacy PIN pairing is not supported.")

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device: str) -> int:
        raise PairingRejected("Passkey entry is not supported.")

    @dbus.service.method(AGENT_INTERFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device: str, passkey: int, entered: int) -> None:
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device: str, pincode: str) -> None:
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device: str, passkey: int) -> None:
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device: str) -> None:
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device: str, uuid: str) -> None:
        if str(uuid).lower() not in ALLOWED_AUDIO_SERVICES:
            raise PairingRejected(f"Bluetooth service {uuid} is not an audio profile.")

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self) -> None:
        return

    def close(self) -> None:
        try:
            self.manager.UnregisterAgent(AGENT_PATH)
        except dbus.DBusException as exc:
            raise RuntimeError(f"BlueZ could not unregister the pairing agent: {exc}") from exc
        finally:
            self.paired_signal.remove()
            self.loop.quit()
            if threading.current_thread() is not self.loop_thread:
                self.loop_thread.join(timeout=2)
            self.remove_from_connection()


def create_pairing_agent() -> BluezPairingAgent:
    try:
        return BluezPairingAgent()
    except (dbus.DBusException, OSError) as exc:
        raise RuntimeError(f"Could not register the automatic Bluetooth pairing agent: {exc}") from exc
