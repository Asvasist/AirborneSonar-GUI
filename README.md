# Airborne Sonar desktop application

Tkinter application for Windows and macOS that configures the transmit signal,
selects the **receiver mode (Standard or Ultrasonic)**, runs one-revolution scans
and stores every microphone capture the board sends. It talks to the firmware
exclusively over Ethernet; no serial connection is needed.

| Port | Transport | Purpose |
|---|---|---|
| 5001 | TCP | Capture records (`ASN3` header + PCM16 WAV payload, CRC32, per-record ACK) |
| 5002 | UDP | Board discovery (`SONARWHO` request, `SONARIP2` reply) |
| 5003 | TCP | Configuration RPC (`ASC2`): chirp parameters, WAV upload, receiver mode |
| 5004 | TCP | Console: firmware log stream and single-key commands |

## 1. Install and run

Pick **one** of the two ways. Both work on Windows and macOS.

### A. Ready-made application (no Python needed)

Build it once in the cloud (section 2), download the file for your computer and
open it:

| Computer | File | How to open |
|---|---|---|
| Windows | `AirborneSonar-Windows.zip` | Unzip, open the folder, double-click `Airborne Sonar.exe`. If SmartScreen warns, choose *More info > Run anyway*. |
| Mac with Apple chip (M1 and newer) | `AirborneSonar-macOS-AppleSilicon.dmg` | Open the disk image and drag **Airborne Sonar** into *Applications*. |
| Mac with Intel chip | `AirborneSonar-macOS-Intel.dmg` | Same as above. |

*Which Mac do I have?* Apple menu > **About This Mac**: "Chip: Apple M..." means
Apple Silicon, "Processor: Intel" means Intel.

The applications are not code-signed. The first time on a Mac, **right-click the
app > Open > Open**. If macOS still refuses, run once in Terminal:

```bash
xattr -dr com.apple.quarantine "/Applications/Airborne Sonar.app"
```

### B. Run from the source folder (first-run installer)

The launcher creates a private virtual environment in the `.venv` folder and
installs the requirements (`numpy`, `scipy`) into it **on the first start only**.
Later starts open the application immediately. Nothing is installed system-wide.

**Windows**

1. Install Python 3.9 or newer from <https://www.python.org/downloads/> (tick
   *Add python.exe to PATH*; keep *tcl/tk and IDLE* enabled).
2. Double-click `Start_Sonar_GUI.cmd`. The first start needs internet and takes a
   few minutes while the environment is created.

**macOS**

1. Install Python 3.9 or newer from <https://www.python.org/downloads/>. Use this
   installer: the Python that ships with macOS or Xcode has an outdated Tk and
   will not work.
2. Open **Terminal**, type `cd ` (with a trailing space), drag the `GUIAPP`
   folder into the window, press Enter, then run:

```bash
bash Start_Sonar_GUI.command
```

   The first start needs internet and takes a few minutes. Afterwards you can
   also double-click `Start_Sonar_GUI.command` (if macOS blocks it, right-click >
   Open once, or run `chmod +x Start_Sonar_GUI.command`).

Manual equivalent of what the launcher does (on Windows use
`.venv\Scripts\python.exe`):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python host/sonar_gui.py
```

## 2. Building the applications (GitHub Actions)

Windows and macOS applications can only be built on their own operating system,
so the repository contains a workflow that builds both on GitHub's machines.

1. Create a new GitHub repository and upload the **contents** of this folder to
   its root (`.github/workflows/build-apps.yml` must end up at the repository
   root, together with `host/`, `tests/` and `requirements.txt`).
2. Open the **Actions** tab > **Build apps** > **Run workflow**.
3. When it finishes (about 10 minutes), open the run and download the artifacts:
   `AirborneSonar-Windows`, `AirborneSonar-macOS-AppleSilicon`,
   `AirborneSonar-macOS-Intel`.

The workflow first runs the receiver-mode and portability tests on Windows and
macOS, then builds the applications with PyInstaller. The Intel macOS entry uses
the `macos-15-intel` runner label; delete that entry from the workflow if GitHub
no longer offers it.

## 3. Connecting the board

1. Program the board (see the top-level README) and connect the Ethernet cable
   directly to the laptop. The board uses a fixed link-local address
   (`169.254.x.x`); the laptop adapter must also have a `169.254.x.x` address.
2. **Windows:** leave the adapter name as `Ethernet` unless Windows calls it
   something else.
3. **Mac:** the Mac needs a wired adapter (USB-C/USB Ethernet). Open
   *System Settings > Network*, select the adapter > *Details > TCP/IP* and set
   *Configure IPv4* to **Using DHCP** (a link-local address is assigned after a
   few seconds when no router answers) or **Link-local only**. The GUI finds the
   `169.254.x.x` interface by itself (the "Interface" box says `auto`; you can
   also type a name such as `en5`). If it picks the wrong one, type the Mac's
   address from the adapter's TCP/IP page into **Laptop IP**.
4. On a Mac the first connection may show a **Local Network** permission request;
   click *Allow*. If it was refused: *System Settings > Privacy & Security >
   Local Network*, enable *Airborne Sonar* (or *Terminal* when started from
   source). Turn off VPNs that block local networks.

## 4. Using the application

1. Choose the recording folder and click **Connect**. The board is discovered
   automatically; Connect only sends status queries.
2. **Receiver mode** (see below): choose Standard or Ultrasonic and click
   **Switch mode**.
3. Choose **Generate chirp** (start/stop frequency, duration, amplitude) or
   **Upload WAV**, then **Apply**. WAV files at other rates are resampled to
   96 kHz before upload.
4. Enter the number of capture positions per revolution (1-1000) and **Start**.
   The board performs one 360-degree scan and stops by itself. **Stop** aborts.

Transmit is limited to 49 ms; each capture is 50 ms of 16-channel audio,
decimated on the board to 96 kHz PCM16 in both receiver modes.

### Receiver mode: Standard / Ultrasonic

| Mode | Microphone clock | Capture | Output |
|---|---|---|---|
| Standard | 2.4 MHz PDM | 60,000 words | 16 channels, 96 kHz, 50 ms |
| Ultrasonic | 3.072 MHz PDM | 76,800 words | 16 channels, 96 kHz, 50 ms |

* The selector shows the mode the board reports. **Switch mode** sends the
  request; the board answers after the microphone clock has restarted.
* Switching is only possible while the board is idle and its DDR queue is empty
  (DDR 0). Stop a running scan and let the queued recordings finish saving first.
* **Start** refuses to run when the selected mode differs from the board's mode,
  so a recording can never be taken in a mode other than the one shown.
* Every capture's JSON sidecar stores `rx_mode` and `pdm_hz`, and the status line
  shows the mode of the last capture.
* Switching modes cancels the board's prepared test identity; the GUI prepares a
  new one automatically on the next Start.
* This needs the dual-mode firmware and a hardware platform that can switch the
  microphone clock. With other firmware the board refuses and the GUI explains
  why; nothing else is affected.

## Recordings

Connecting or receiving an idle batch marker creates no folders in the chosen
recording directory. Applied audio settings are held in a temporary cache until
a test produces data.

Every verified **Start** reserves a new `test_<timestamp>_<id>/` destination.
The folder is created only when the first valid completed capture arrives:

```text
SonarCaptures/   (default: D:\SonarCaptures on Windows, ~/SonarCaptures elsewhere)
    test_<timestamp>_<id>/
        test.json
        configurations/
            config_00001.json
            config_00001_source.wav     # uploaded-WAV mode
            config_00001_playback.wav   # uploaded-WAV mode
        second_<capture_time>/
            capture_0000000001.wav
            capture_0000000001.json     # includes rx_mode and pdm_hz
```

Every capture is stored with its JSON sidecar before the board receives its
acknowledgement, so an unacknowledged frame is retransmitted rather than lost.
Each test includes a snapshot of its applied transmit configuration and uploaded
WAV assets, keeping automatic analyzer mode functional. Idle markers are still
acknowledged but no longer create `second_*` folders or `batch.json` files.

**Stop** keeps the receiver open to save queued captures into that same test.
**Start** stays disabled until the board is idle/stopped, its status is fresh,
and DDR reports zero queued frames. The next Start reserves another test folder,
even if the audio settings are unchanged. Delayed records and identical retries
are routed by their capture sequence rather than the time they reach the laptop.
The `test.json` status is finalized when the board stops and its queue is empty.

Completed audio is saved as it is delivered rather than held until a full
revolution or several revolutions complete; the board has a bounded 32-slot
queue. The existing firmware still transfers in 30-second batches, so files can
appear after movement finishes or after Stop. This GUI update does not alter
motor control, transmit/receive timing, the filter, or the firmware protocol.

If retained frames arrive from an earlier connection without a known test,
they are preserved in a separate `recovered_<timestamp>_<id>/` folder. No audio
is acknowledged as saved before it is durably written. Unknown older frames
may lack their transmit configuration; use their original saved settings when
analyzing them. A board reboot starts a new sequence namespace without changing
previously saved test folders.

The board sends data in 30-second batches. Wait for the status line to show
`STOPPED` with an empty DDR queue before resetting the board; unsent captures
live in volatile RAM.

Command-line tools for headless use (use `py -3` instead of `python3` on Windows):

```bash
python3 host/receive_ethernet.py --output ~/SonarCaptures
python3 host/sonar_configure.py --help
python3 host/sonar_configure.py mode ultrasonic      # switch receiver mode
python3 host/plot_ethernet.py <capture.bin>          # needs: pip install -r host/requirements-plot.txt
```

## Code structure

| Module | Responsibility |
|---|---|
| `sonar_gui.py` | Tk widgets and event loop; the only module that touches Tk |
| `sonar_gui_control.py` | Start/stop sequencing and status parsing, no I/O |
| `sonar_gui_audio.py` | Audio validation and the cancellable configuration worker (also the mode switch) |
| `sonar_console_transport.py` | TCP 5004 console framing and worker thread |
| `sonar_gui_transport.py` | Capture receiver thread with reconnect |
| `sonar_test_store.py` | Lazy test folders, sequence-based routing and configuration snapshots |
| `receive_ethernet.py`, `capture_store.py` | Record protocol and atomic persistence |
| `sonar_configure.py`, `sonar_wav_format.py` | Configuration RPC client (`set_mode`, `board_mode`) and WAV checks/resampling |
| `sonar_connection.py` | Adapter selection (Windows PowerShell, macOS `ifconfig`, Linux `ip`) and board discovery |
| `sonar_platform.py` | Per-OS defaults: recording folder, fonts, opening folders |

Worker threads never call Tk; they post events to a queue drained by the
UI thread.

## Tests

```bash
.venv/bin/python -m unittest tests.test_mode_platform -v     # Windows: .venv\Scripts\python.exe
```

`tests/test_mode_platform.py` covers the receiver-mode request and its refusals,
the interface parsers for macOS and Linux, the recording header's mode field and
the selector's Start guard. It uses fake sockets and a hidden Tk window; it never
opens a network connection or moves the motor.

The older suites in `tests/` (`discover -s tests`) were written for an earlier
protocol revision and report failures unrelated to the receiver mode; they are
not part of the cloud workflow.
