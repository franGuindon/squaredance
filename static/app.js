const statusLight = document.querySelector("#status-light");
const connectionState = document.querySelector("#connection-state");
const adapterName = document.querySelector("#adapter-name");
const startButton = document.querySelector("#start-button");
const stopButton = document.querySelector("#stop-button");
const refreshButton = document.querySelector("#refresh-button");
const recordStartButton = document.querySelector("#record-start-button");
const recordStopButton = document.querySelector("#record-stop-button");
const recordingState = document.querySelector("#recording-state");
const recordingFile = document.querySelector("#recording-file");
const feedback = document.querySelector("#feedback");
const deviceList = document.querySelector("#device-list");
const outputList = document.querySelector("#output-list");
const pairingNote = document.querySelector("#pairing-note");

function item(text, className = "") {
  const li = document.createElement("li");
  li.textContent = text;
  if (className) li.className = className;
  return li;
}

function renderStatus(status, message = null) {
  const adapter = status.adapter;
  const connectedDevice = status.devices.find((device) => device.connected);
  const connected = Boolean(connectedDevice);
  const recording = status.recording || { active: false, file: null, error: null };
  const discoverable = adapter.discoverable && !connected;
  statusLight.classList.toggle("connected", connected);
  statusLight.classList.toggle("discoverable", discoverable);
  connectionState.textContent = connected
    ? `Connected to ${connectedDevice.name}`
    : adapter.discoverable
      ? "Discoverable · no device connected"
      : "No device connected";
  const speakerClass = status.speaker_class_active
    ? " · Loudspeaker profile"
    : status.device_class
      ? ` · Bluetooth class ${status.device_class}`
      : "";
  adapterName.textContent = `${adapter.name} · ${adapter.powered ? "Bluetooth on" : "Bluetooth off"}${speakerClass}`;
  startButton.textContent = status.speaker_mode ? "Start speaker mode" : "Set up speaker mode";
  stopButton.hidden = !adapter.discoverable && !adapter.pairable;
  pairingNote.textContent = adapter.discoverable
    ? "Discoverable; automatic pairing is enabled."
    : "";
  recordStartButton.disabled = !connected || recording.active;
  recordStopButton.disabled = !recording.active;
  recordingState.textContent = recording.active ? "Recording" : "Not recording";
  recordingState.classList.toggle("recording", recording.active);
  recordingFile.textContent = recording.error
    ? `Recording error: ${recording.error}`
    : recording.file
      ? `${recording.active ? "Saving to" : "Saved to"} ${recording.file}`
      : "Recordings will be saved in ~/Music/Computer Speaker.";

  deviceList.replaceChildren();
  if (status.devices.length) {
    for (const device of status.devices) {
      const row = document.createElement("li");
      const name = document.createElement("span");
      const cannotSendAudio = device.audio_role === "sink";
      const action = document.createElement(
        device.connected || cannotSendAudio ? "span" : "button"
      );
      name.textContent = device.name;
      action.className = device.connected
        ? "device-state"
        : cannotSendAudio
          ? "device-direction"
          : "device-connect";
      action.textContent = device.connected
        ? "Connected"
        : cannotSendAudio
          ? "Receiver"
          : "Connect";
      if (cannotSendAudio && !device.connected) {
        action.title =
          "This device advertises an A2DP receiver profile. Switch it to Bluetooth transmit mode to send audio to the laptop.";
      }
      if (!device.connected && !cannotSendAudio) {
        action.type = "button";
        action.addEventListener("click", () =>
          sendAction(
            `/api/device/${encodeURIComponent(device.address)}/connect`,
            `Connecting to ${device.name}…`
          )
        );
      }
      row.append(name, action);
      deviceList.append(row);
    }
  } else {
    deviceList.append(item("No paired devices yet.", "muted"));
  }

  const turntable = status.devices.find(
    (device) => device.name.toLowerCase() === "jta-245"
  );
  if (turntable?.audio_role === "sink" && !turntable.connected) {
    const hint = item(
      "JTA-245 is currently advertising Bluetooth receiver mode (A2DP Sink). Switch the turntable to Bluetooth TX/transmit mode so it sends audio to this laptop, then scan or reconnect.",
      "device-hint"
    );
    hint.setAttribute("role", "note");
    deviceList.append(hint);
  }

  outputList.replaceChildren();
  if (status.audio_outputs.length) {
    status.audio_outputs.forEach((output) => outputList.append(item(output)));
  } else {
    outputList.append(item(status.audio_error || "No audio output detected.", "muted"));
  }
  if (feedback.dataset.error !== "true") {
    feedback.textContent =
      message ||
      (status.pairing_mode && !status.speaker_class_active
        ? "Speaker mode is on, but the Bluetooth adapter is no longer advertising the Loudspeaker class. Restart speaker mode and approve the system prompt."
        : status.pairing_mode
          ? "Ready for nearby Bluetooth audio devices to pair and connect."
          : status.speaker_mode
            ? "Bluetooth speaker support is configured."
            : "Speaker support has not been configured yet.");
  }
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || "Could not read Bluetooth status.");
    renderStatus(status);
  } catch (error) {
    feedback.textContent = error.message;
    feedback.classList.add("error");
    feedback.dataset.error = "true";
  }
}

async function sendAction(path, successMessage) {
  startButton.disabled = true;
  stopButton.disabled = true;
  feedback.classList.remove("error");
  feedback.dataset.error = "false";
  feedback.textContent = "Updating Bluetooth and audio settings…";
  try {
    const response = await fetch(path, { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The request could not be completed.");
    feedback.textContent = successMessage;
    renderStatus(result, successMessage);
  } catch (error) {
    feedback.textContent = error.message;
    feedback.classList.add("error");
    feedback.dataset.error = "true";
  } finally {
    startButton.disabled = false;
    stopButton.disabled = false;
  }
}

async function sendRecordingAction(path, successMessage) {
  recordStartButton.disabled = true;
  recordStopButton.disabled = true;
  feedback.classList.remove("error");
  feedback.dataset.error = "false";
  feedback.textContent = "Updating recording…";
  try {
    const response = await fetch(path, { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The recording request failed.");
    renderStatus(result, successMessage);
  } catch (error) {
    feedback.textContent = error.message;
    feedback.classList.add("error");
    feedback.dataset.error = "true";
    await refreshStatus();
  }
}

startButton.addEventListener("click", () =>
  sendAction(
    "/api/speaker/start",
    "Speaker mode is ready. Put the JTA-245 in Bluetooth TX/transmit mode, then scan from the turntable."
  )
);
stopButton.addEventListener("click", () =>
  sendAction("/api/speaker/stop", "Speaker mode and new pairing are off. Existing connections are left untouched.")
);
refreshButton.addEventListener("click", refreshStatus);
recordStartButton.addEventListener("click", () =>
  sendRecordingAction("/api/recording/start", "Recording started.")
);
recordStopButton.addEventListener("click", () =>
  sendRecordingAction("/api/recording/stop", "Recording saved as a WAV file.")
);

refreshStatus();
window.setInterval(refreshStatus, 5000);
