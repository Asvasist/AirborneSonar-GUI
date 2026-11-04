"""Board command sequencing. No GUI or socket dependencies."""
import re
import time

STATUS = re.compile(r"SONAR (IDLE|WAIT_BUFFER|ACQUIRE|PRE_MOVE|MOVING|SETTLING|STOPPED|FAULT) "
                    r"cycle=(\d+) steps=(-?\d+) divisor=(\d+) receiver=(\d+) motor_pos=(-?\d+) DDR=(\d+)/(\d+)")
IDLE_STATES = {"IDLE", "STOPPED"}
RX_MODE = re.compile(r"rx_mode=(STANDARD|ULTRASONIC) pdm_hz=(\d+)")
AUDIO_STATUS = re.compile(r"config=(\d+) mode=(GENERATE|WAV) tx_us=(\d+) amplitude=(\d+) waveform=(\d+) audio_ready=(\d+)")
SWEEP = re.compile(r"sweep_deg=(\d+)")  # Absent in firmware without the sweep option.
FULL_TURN = 360
SETUP_PHASES = {"sweep_prompt", "sweep_value", "prompt", "value"}


def parse_steps(text, reverse=False):
    """Parse capture positions per complete 360-degree revolution.

    The firmware divides one calibrated revolution into N moves.
    A negative value is sent when reverse direction is selected.
    """
    value = str(text).strip()
    if not re.fullmatch(r"[0-9]{1,4}", value) or not 1 <= int(value) <= 1000:
        raise ValueError("Enter a whole number of positions from 1 to 1000.")
    return -int(value) if reverse else int(value)


def parse_sweep(text):
    """Sweep angle in whole degrees: 360 = one turn, less = partial sweep."""
    value = str(text).strip()
    if not re.fullmatch(r"[0-9]{1,3}", value) or not 1 <= int(value) <= FULL_TURN:
        raise ValueError("Enter a whole sweep angle from 1 to 360 degrees.")
    return int(value)


def recordings(steps, sweep):
    """360 degrees: one recording per step. Less: both ends are recorded."""
    return abs(steps) if sweep == FULL_TURN else abs(steps) + 1


class ExperimentControl:
    def __init__(self, send, notify, now=time.monotonic, before_run=None):
        self.send, self.notify, self.now = send, notify, now
        self.before_run = before_run
        self.console_up = self.ethernet_up = False
        self.receiver_confirmed = self.fault_latched = False
        self.board_state = "UNKNOWN"
        self.phase = "idle"
        self.steps = self.divisor = self.cycle = self.ddr = self.capacity = 0
        self.requested = 0
        self.sweep = FULL_TURN
        self.board_sweep = None  # Reported by the status line; None for older firmware.
        self.stored = False
        self.deadline = 0.0
        self.last_status = 0.0
        self.last_query = 0.0
        self.error = ""
        self.applied_audio = None
        self.reported_audio = None
        self.rx_mode = None  # Reported by the board's status line; None until known.

    @property
    def can_configure(self):
        return (self.console_up and self.ethernet_up and self.receiver_confirmed and not self.fault_latched and self.phase == "idle" and
                self.board_state in IDLE_STATES and self.now() - self.last_status < 4.0)

    @property
    def audio_matches(self):
        a, r = self.applied_audio, self.reported_audio
        return bool(a and r and r["ready"] and a["config_id"] == r["config_id"]
                    and a["mode"] == r["mode"] and a["duration_us"] == r["duration_us"]
                    and a["amplitude_pct"] == r["amplitude_pct"] and a["waveform_id"] == r["waveform_id"])

    @property
    def can_start(self):
        return self.can_configure and self.audio_matches and self.ddr == 0

    def audio_configuration(self, value):
        self.applied_audio = dict(value) if value else None
        if value:
            self.last_status = 0.0  # Require a fresh live-console confirmation after TCP apply.

    @property
    def drained(self):
        return (self.console_up and self.board_state in IDLE_STATES and self.ddr == 0 and
                self.phase == "idle" and self.now() - self.last_status < 4.0)

    def console_connection(self, up):
        self.audio_configuration(None)
        self.reported_audio = None
        self.rx_mode = None
        self.board_sweep = None
        self.console_up = up
        self.board_state = "UNKNOWN"
        self.phase = "idle"
        self.receiver_confirmed = False
        self.last_status = 0.0
        if up:
            self.fault_latched = False
            self.error = ""
            self.send(b"s")
            self.last_query = self.now()
        else:
            self.phase = "idle"
            self.error = "Ethernet console disconnected. The firmware requests Stop on connection loss; reconnect to confirm."

    def ethernet_connection(self, up):
        self.ethernet_up = up
        if not up:
            self.audio_configuration(None)
        if not up and self.phase in SETUP_PHASES | {"starting"}:
            self.error = "Start cancelled because Ethernet disconnected."
            self.stop()

    def start(self, text, reverse=False, sweep=FULL_TURN):
        steps = parse_steps(text, reverse)
        sweep = parse_sweep(sweep)
        if not self.can_start:
            raise ValueError("Apply audio settings and wait for matching idle-board/Ethernet confirmations and an empty DDR queue before starting.")
        if sweep != FULL_TURN and self.board_sweep is None:
            raise ValueError("This board firmware only scans 360 degrees. Load the Vitis firmware with the sweep option.")
        self.error = ""
        self.requested = steps
        self.sweep = sweep
        self.stored = False
        self.deadline = self.now() + 10.0
        # The board keeps its sweep between tests; send it only when it differs.
        if self.board_sweep is not None and self.board_sweep != sweep:
            self.phase = "sweep_prompt"
            self.send(b"g")
            self.notify(f"Setting the sweep angle to {sweep} degrees; waiting for the board's prompt.")
        else:
            self.ask_positions()

    def ask_positions(self):
        self.phase = "prompt"
        self.deadline = self.now() + 10.0
        self.send(b"n")
        self.notify("Setting the number of steps; waiting for the board's prompt.")

    def stop(self):
        if not self.console_up:
            raise ValueError("Ethernet console is disconnected; Stop cannot be confirmed until reconnection.")
        self.phase = "stopping"
        self.deadline = self.now() + 10.0
        # Transport gives X priority over queued status/settings commands.
        self.send(b"x")
        self.last_query = 0.0
        self.notify("Stop requested. Receiver stays open to save queued recordings.")

    def line(self, line):
        if line.startswith("INIT:"):
            self.audio_configuration(None)
            self.reported_audio = None
            self.rx_mode = None
            self.board_state = "UNKNOWN"
            self.phase = "idle"
            self.receiver_confirmed = False
            self.fault_latched = False
            self.last_status = 0.0
            return
        if "SONAR FAULT:" in line or "HEALTH FAULT" in line or "SONAR not ready:" in line:
            self.fault_latched = True
            self.board_state = "FAULT"
            self.phase = "idle"
            self.error = line
            return
        if self.phase == "sweep_prompt" and "Enter sweep angle in whole degrees (1..360), then Enter:" in line:
            if not self.ethernet_up:
                self.stop()
                return
            self.send(f"{self.sweep}\r".encode("ascii"))
            self.phase = "sweep_value"
            self.deadline = self.now() + 10.0
            return
        if self.phase == "sweep_value" and "Sweep stored:" in line:
            self.ask_positions()
            return
        if self.phase == "prompt" and "Enter signed positions per 360 degrees (1..1000), then Enter:" in line:
            if not self.ethernet_up:
                self.stop()
                return
            self.send(f"{self.requested}\r".encode("ascii"))
            self.phase = "value"
            self.deadline = self.now() + 10.0
        if self.phase == "value" and "Positions-per-revolution stored" in line:
            self.stored = True
        if self.phase in SETUP_PHASES | {"starting"} and (
                "Invalid or rejected value" in line or "Cannot start:" in line):
            self.error = line
            self.stop()
            return
        match = STATUS.search(line)
        if not match:
            return
        state, cycle, steps, divisor, receiver, position, used, capacity = match.groups()
        if int(cycle) < self.cycle:
            self.audio_configuration(None)
        audio = AUDIO_STATUS.search(line)
        mode = RX_MODE.search(line)
        if mode:
            self.rx_mode = mode.group(1)
        sweep = SWEEP.search(line)
        self.board_sweep = int(sweep.group(1)) if sweep else None
        self.reported_audio = None
        if audio:
            config, mode, duration, amplitude, waveform, ready = audio.groups()
            self.reported_audio = dict(config_id=int(config), mode=1 if mode == "WAV" else 0,
                                       duration_us=int(duration), amplitude_pct=int(amplitude),
                                       waveform_id=int(waveform), ready=ready == "1")
        self.board_state = state
        self.cycle, self.steps, self.divisor = int(cycle), int(steps), int(divisor)
        self.ddr, self.capacity = int(used), int(capacity)
        self.receiver_confirmed = receiver == "1"
        self.last_status = self.now()
        if state == "FAULT":
            self.fault_latched = True
            self.phase = "idle"
            self.error = "Board fault. Read the activity log; reset in Vitis only after saving queued captures."
        elif self.phase == "value" and self.stored:
            if (state not in IDLE_STATES or self.steps != self.requested or not self.ethernet_up
                    or receiver != "1" or not self.audio_matches or self.ddr != 0
                    or (self.board_sweep or FULL_TURN) != self.sweep):
                self.error = "Board positions/audio/receiver confirmation did not match; start cancelled."
                self.stop()
            else:
                try:
                    if self.before_run is not None:
                        self.before_run(self.cycle + 1, self.requested)
                except (OSError, ValueError) as error:
                    self.error = f"Recording test could not be prepared: {error}"
                    self.stop()
                    return
                self.send(b"r")
                self.phase = "starting"
                self.deadline = self.now() + 10.0
                self.notify("Scan positions confirmed and receiver connected. Start sent.")
        elif self.phase == "starting" and state not in IDLE_STATES:
            self.phase = "idle"
            if self.sweep == FULL_TURN:
                self.notify("360-degree scan running. The board stops automatically after one revolution.")
            else:
                self.notify(f"{self.sweep}-degree sweep running ({recordings(self.requested, self.sweep)} recordings). "
                            "The board stops automatically at the end angle.")
        elif self.phase == "stopping" and state == "STOPPED":
            self.phase = "idle"
            self.notify("Board stopped. Waiting for queued recordings to transfer." if self.ddr else "Board stopped; DDR is empty.")

    def tick(self):
        now = self.now()
        if self.console_up and self.phase not in SETUP_PHASES and now - self.last_query >= 1.0:
            self.send(b"s")
            self.last_query = now
        if self.phase != "idle" and now >= self.deadline:
            if self.phase == "stopping":
                self.phase = "idle"
                self.board_state = "UNKNOWN"
                self.error = "Stop has not been confirmed. Check the Ethernet console and board."
                self.notify(self.error)
            else:
                self.error = "Timed out waiting for board confirmation; start cancelled."
                self.stop()
