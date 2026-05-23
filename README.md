# Corporate Translator 🪪➡️🤵

A Raspberry Pi Zero 2W that plugs into a Windows PC as a USB dongle. Highlight
some plain text, press the button, and it instantly rewrites your selection into
glorious corporate jargon — *"let's talk later"* becomes *"let's circle back and
synergize on this deliverable when we have the bandwidth."*

It works without installing any drivers on the PC: the Pi presents itself as a
standard **USB HID keyboard** and a **USB CDC ACM serial port**.

## How it works

When you press the button on the Pi:

1. **Ctrl+C** is sent over USB HID → the PC copies your selected text.
2. The Pi asks a small **companion script** on the PC for the clipboard contents
   over USB serial.
3. The Pi calls the **Claude API** (`claude-haiku-4-5`) over WiFi to translate
   the text into corporate jargon.
4. The translation is sent back to the companion, which **sets the clipboard**.
5. **Ctrl+V** is sent over USB HID → the translation pastes over your selection.

```
 [PC selection] --Ctrl+C--> clipboard
        Pi --GET_CLIP--> companion --CLIP:<b64>--> Pi
        Pi --(Claude API over WiFi)--> corporate jargon
        Pi --SET_CLIP:<b64>--> companion --> clipboard
 [PC selection] <--Ctrl+V-- translation
```

## Hardware

- Raspberry Pi Zero 2W — connect the **USB (OTG) data port** to the PC (not the
  PWR-only port).
- One tactile button between **GPIO16 (BCM)** and **GND** (active-low; the
  internal pull-up is enabled in software).
- *Optional:* a [WhisPlay HAT](https://www.waveshare.com/) for RGB LED feedback.
  If it isn't present the program runs fine without it.

## LED feedback (WhisPlay HAT)

| LED                | Meaning                  |
|--------------------|--------------------------|
| Breathing green    | Idle / ready             |
| Solid amber        | Waiting for the clipboard|
| Pulsing yellow     | Translating              |
| 3× green flash     | Success                  |
| 3× red flash       | Error                    |

## File layout

```
pi-corporate-translator/
├── main.py                 # button + LED state machine, serial comms, orchestration
├── translator.py           # Claude API call using the OAuth token in ~/.claude/.credentials.json
├── hid_keyboard.py         # writes HID reports to /dev/hidg0 (Ctrl+C / Ctrl+V)
├── setup_gadget.sh         # configures the USB composite gadget via libcomposite at boot
├── corp-translator.service # systemd unit: setup_gadget.sh then main.py
├── requirements.txt        # requests, pyserial
├── README.md               # this file
└── companion/
    ├── companion.py        # Windows: relays clipboard over serial (pyserial + pywin32)
    └── requirements.txt     # pyserial, pywin32
```

---

## Pi setup

### 1. Enable USB gadget mode

Edit the boot config (on Bookworm this is `/boot/firmware/config.txt`; on older
images `/boot/config.txt`) and add:

```ini
dtoverlay=dwc2,dr_mode=peripheral
```

Add the required modules to `/etc/modules`:

```
dwc2
libcomposite
```

### 2. Install the project

```bash
sudo apt update && sudo apt install -y python3-pip git
git clone https://github.com/raiuRobert/pi-corporate-translator.git
cd pi-corporate-translator
pip3 install -r requirements.txt --break-system-packages
chmod +x setup_gadget.sh
```

> If you use the WhisPlay HAT, also install its Python library per the vendor's
> instructions. It's optional — the code skips it gracefully if missing.

### 3. Log in to Claude

`translator.py` reads the OAuth token written by the Claude CLI to
`~/.claude/.credentials.json`. Install and log in once:

```bash
claude   # follow the prompts to authenticate
```

The token is refreshed automatically when it expires.

### 4. Install the service

The unit assumes the repo lives at `/home/pi/pi-corporate-translator`. Adjust the
paths in `corp-translator.service` if you cloned elsewhere, then:

```bash
sudo cp corp-translator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now corp-translator.service
journalctl -u corp-translator.service -f   # watch the logs
```

`setup_gadget.sh` runs first (it's idempotent — it tears down any existing
gadget before recreating it), then `main.py` starts and waits for button
presses.

---

## Windows companion setup

The PC needs a tiny relay running so the Pi can read and write the clipboard.

```powershell
cd companion
pip install -r requirements.txt    # pyserial + pywin32
python companion.py
```

It auto-detects the Pi's COM port (by matching "USB Serial" / "CDC" / "ACM" in
the port description) and reconnects automatically if the dongle is unplugged.

### Auto-start the companion

**Startup folder (simplest):** create a shortcut to
`pythonw.exe "C:\path\to\companion\companion.py"` and drop it in the folder that
opens when you run `shell:startup` (Win+R → `shell:startup`). Using `pythonw.exe`
runs it without a console window.

**Task Scheduler (more robust):** create a task that runs at logon, action
`python.exe` with argument `C:\path\to\companion\companion.py`, "Run whether user
is logged on or not" optional.

---

## Usage

The companion registers a global hotkey on the PC. There are two ways to
trigger a translation:

**Hotkey (recommended): Ctrl+Alt+T from any focused app with text selected.**
Companion handles the whole cycle locally (~3 s, no dongle required) using
the PC's own Claude credentials. This is the default mode and works whether
the Pi is plugged in or not.

**Physical button on the Pi.** Press the GPIO16 button; the Pi sends HID
Ctrl+C, asks the companion for the clipboard over serial, translates,
sends back, then HID Ctrl+V. Same UX, just dongle-driven.

To make the hotkey route through the Pi instead of running locally,
set `COMPANION_MODE=dongle` in the companion's environment and run the
Pi with `main.py --trigger serial`. Useful if you specifically want the
dongle to do the keystroking (e.g., to leave no fingerprints from the
PC's own keyboard process).

In all cases:
1. Make sure `companion.py` is running on the PC.
2. Select your text in any app.
3. Press Ctrl+Alt+T (or the Pi button).
4. Watch your prose get *operationalized*. ✨

## Testing without a button

Validate the build in layers, from "needs no hardware" upward:

**1. Translator only (no button, no PC needed) — just WiFi + login.** This is the
core. On the Pi:

```bash
python3 translator.py "let's talk later about the project"
```

It should print a corporate-jargon rewrite. If it errors with `No accessToken`,
run `claude` to log in first.

**2. USB gadget devices.** Bring the gadget up and confirm the device nodes
appear (no PC interaction required):

```bash
sudo bash setup_gadget.sh
ls -l /dev/hidg0 /dev/ttyGS0
```

**3. Full pipeline without a button.** Plug the Pi's USB data port into the PC,
start `companion.py` on the PC, then run the main program in keyboard mode and
press **Enter** to trigger a cycle (instead of the physical button):

```bash
sudo python3 main.py --trigger key
```

Select text on the PC, switch to the Pi's terminal, press Enter, and watch it
copy → translate → paste. `sudo python3 main.py --once` runs a single cycle and
exits. When you later wire a button to GPIO16, run `main.py` with no flags (or
use the systemd service).

## Troubleshooting

- **Nothing happens on button press** — check `journalctl -u corp-translator`.
  Confirm the button is on **GPIO16** to **GND** and that `/dev/hidg0` exists
  (`ls /dev/hidg0`); if not, the gadget didn't come up — see below.
- **No `/dev/hidg0` or `/dev/ttyGS0`** — verify `dtoverlay=dwc2,dr_mode=peripheral`
  is in the boot config and `dwc2` + `libcomposite` are in `/etc/modules`, then
  reboot. Make sure you're using the **data** USB port on the Pi.
- **Companion can't find the port** — check Device Manager for the COM port under
  "Ports (COM & LPT)"; the description must contain "USB Serial", "CDC", or "ACM".
  Re-plug the dongle.
- **`No accessToken` / `Token refresh failed`** — run `claude` on the Pi to log in
  again so `~/.claude/.credentials.json` is populated/valid.
- **Translation pastes nothing or the original** — the clipboard round-trip timed
  out (`CLIP_TIMEOUT_S = 10` in `main.py`). Confirm the companion is running and
  connected.
- **Wrong text gets translated** — the dongle copies whatever is *selected* at the
  moment you press the button; make sure your selection is active in the focused
  app.
