r"""Corporate Translator -- main orchestration loop.

Runs on a Raspberry Pi Zero 2W configured as a USB HID keyboard + CDC ACM
serial gadget (see ``setup_gadget.sh``). A single button press drives the flow:

    IDLE -> COPYING -> WAITING_CLIP -> TRANSLATING -> PASTING -> SUCCESS -> IDLE
                                                              \-> ERROR -> IDLE

  COPYING       send Ctrl+C to the host (copies the selected text)
  WAITING_CLIP  ask the companion for the clipboard over serial
  TRANSLATING   call the Claude API to rewrite it as corporate jargon
  PASTING       push the result back to the clipboard, send Ctrl+V

Serial protocol (newline-delimited, clipboard text is base64 UTF-8):
    Pi  -> companion : GET_CLIP
    companion -> Pi  : CLIP:<base64>
    Pi  -> companion : SET_CLIP:<base64>

LED feedback is provided by the WhisPlay HAT when present; it is imported under
try/except so the program also runs headless.
"""

import base64
import threading
import time

import serial

import hid_keyboard
from translator import translate, TranslationError

# --- Configuration -------------------------------------------------------
BUTTON_GPIO = 16            # BCM numbering, active-low (wired to GND)
SERIAL_PORT = "/dev/ttyGS0"
SERIAL_BAUD = 115200
CLIP_TIMEOUT_S = 10         # how long to wait for CLIP: after GET_CLIP
DEBOUNCE_S = 0.20

# --- States --------------------------------------------------------------
IDLE = "IDLE"
COPYING = "COPYING"
WAITING_CLIP = "WAITING_CLIP"
TRANSLATING = "TRANSLATING"
PASTING = "PASTING"
SUCCESS = "SUCCESS"
ERROR = "ERROR"


# --- Optional WhisPlay HAT LED ------------------------------------------
class _NullLed:
    """No-op LED used when the WhisPlay HAT is not installed."""

    def idle(self): pass
    def waiting(self): pass
    def translating(self): pass
    def success(self): pass
    def error(self): pass
    def stop(self): pass


def _make_led():
    try:
        from whisplay import WhisPlay  # type: ignore
    except Exception:
        return _NullLed()

    try:
        return _WhisplayLed(WhisPlay())
    except Exception:
        return _NullLed()


class _WhisplayLed:
    """Animated RGB feedback on a WhisPlay HAT, driven by a background thread.

    Modes: breathing green (idle), solid amber (waiting), pulsing yellow
    (translating), 3x green flash (success), 3x red flash (error).
    """

    def __init__(self, hat):
        self._hat = hat
        self._mode = "idle"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _set_mode(self, mode):
        with self._lock:
            self._mode = mode

    def idle(self): self._set_mode("idle")
    def waiting(self): self._set_mode("waiting")
    def translating(self): self._set_mode("translating")
    def success(self): self._set_mode("success")
    def error(self): self._set_mode("error")

    def stop(self):
        self._stop.set()
        try:
            self._hat.set_color(0, 0, 0)
        except Exception:
            pass

    def _color(self, r, g, b):
        try:
            self._hat.set_color(int(r), int(g), int(b))
        except Exception:
            pass

    def _breathe(self, base, period=2.0):
        t = time.time() % period
        level = (1 - abs(2 * t / period - 1))  # 0->1->0 triangle
        self._color(base[0] * level, base[1] * level, base[2] * level)

    def _flash(self, color, times=3):
        for _ in range(times):
            self._color(*color)
            time.sleep(0.15)
            self._color(0, 0, 0)
            time.sleep(0.15)

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                mode = self._mode
            if mode == "idle":
                self._breathe((0, 80, 0), period=3.0)
                time.sleep(0.03)
            elif mode == "waiting":
                self._color(255, 140, 0)  # solid amber
                time.sleep(0.05)
            elif mode == "translating":
                self._breathe((180, 180, 0), period=0.8)  # pulsing yellow
                time.sleep(0.02)
            elif mode == "success":
                self._flash((0, 200, 0))
                self._set_mode("idle")
            elif mode == "error":
                self._flash((200, 0, 0))
                self._set_mode("idle")
            else:
                time.sleep(0.05)


# --- Serial clipboard relay ---------------------------------------------
class ClipboardLink:
    """Talks to the Windows companion over the CDC ACM serial line."""

    def __init__(self, port=SERIAL_PORT, baud=SERIAL_BAUD):
        self._ser = serial.Serial(port, baud, timeout=0.2)
        self._clip_event = threading.Event()
        self._clip_value = None
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception:
                time.sleep(0.2)
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle_line(line.strip())

    def _handle_line(self, line: bytes):
        if line.startswith(b"CLIP:"):
            payload = line[len(b"CLIP:"):]
            try:
                self._clip_value = base64.b64decode(payload).decode("utf-8")
            except Exception:
                self._clip_value = ""
            self._clip_event.set()

    def get_clipboard(self, timeout=CLIP_TIMEOUT_S):
        """Request the host clipboard. Returns text or raises TimeoutError."""
        self._clip_event.clear()
        self._clip_value = None
        self._ser.write(b"GET_CLIP\n")
        self._ser.flush()
        if not self._clip_event.wait(timeout):
            raise TimeoutError("Companion did not return the clipboard in time.")
        return self._clip_value or ""

    def set_clipboard(self, text: str):
        payload = base64.b64encode(text.encode("utf-8"))
        self._ser.write(b"SET_CLIP:" + payload + b"\n")
        self._ser.flush()

    def close(self):
        self._stop.set()
        try:
            self._ser.close()
        except Exception:
            pass


# --- Translation flow ----------------------------------------------------
def run_flow(link: ClipboardLink, led):
    """Execute one full copy -> translate -> paste cycle."""
    try:
        # COPYING
        hid_keyboard.send_copy()
        time.sleep(0.25)  # let the host update its clipboard

        # WAITING_CLIP
        led.waiting()
        original = link.get_clipboard()

        # TRANSLATING
        led.translating()
        rewritten = translate(original)

        # PASTING
        link.set_clipboard(rewritten)
        time.sleep(0.15)
        hid_keyboard.send_paste()

        led.success()
        print("OK: %r -> %r" % (original[:60], rewritten[:60]))
    except (TranslationError, TimeoutError, OSError) as exc:
        led.error()
        print("ERROR: %s" % exc)
    except Exception as exc:  # noqa: BLE001 - never let the worker thread kill the loop
        led.error()
        print("UNEXPECTED ERROR: %s" % exc)


# --- Trigger handling ----------------------------------------------------
def _make_trigger(busy, link, led):
    """Return a callable that runs one flow if one isn't already in progress."""

    def on_press():
        if not busy.acquire(blocking=False):
            return  # a flow is already running; ignore the trigger
        try:
            run_flow(link, led)
        finally:
            led.idle()
            busy.release()

    return on_press


def _loop_button(on_press):
    """Poll the GPIO button (active-low) and fire on_press for each press."""
    import RPi.GPIO as GPIO  # imported here so the module stays importable off-Pi

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print("Corporate Translator ready. Press the button on GPIO%d." % BUTTON_GPIO)
    last = 0.0
    try:
        while True:
            if GPIO.input(BUTTON_GPIO) == GPIO.LOW:  # active-low
                now = time.time()
                if now - last > DEBOUNCE_S:
                    last = now
                    threading.Thread(target=on_press, daemon=True).start()
                while GPIO.input(BUTTON_GPIO) == GPIO.LOW:  # wait for release
                    time.sleep(0.02)
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()


def _loop_keyboard(on_press):
    """Trigger a flow each time Enter is pressed -- a no-button test harness."""
    print("Corporate Translator ready (keyboard mode). "
          "Press Enter to translate, Ctrl+C to quit.")
    try:
        while True:
            input()  # blocks until Enter
            threading.Thread(target=on_press, daemon=True).start()
    except (KeyboardInterrupt, EOFError):
        pass


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Corporate Translator dongle")
    parser.add_argument(
        "--trigger", choices=("button", "key"), default="button",
        help="how to start a translation: physical button (default) or Enter key",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run a single translation flow and exit (no trigger loop)",
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="seconds to wait before starting (gives you time to select text)",
    )
    args = parser.parse_args()

    led = _make_led()
    led.idle()
    hid_keyboard.release_all()  # clear any key left stuck by a previous crash
    link = ClipboardLink()
    busy = threading.Lock()
    on_press = _make_trigger(busy, link, led)

    try:
        if args.once:
            if args.delay:
                print("Select your text now -- translating in %g seconds..."
                      % args.delay)
                time.sleep(args.delay)
            on_press()
        elif args.trigger == "key":
            _loop_keyboard(on_press)
        else:
            _loop_button(on_press)
    finally:
        hid_keyboard.release_all()  # never exit with a key held down
        led.stop()
        link.close()


if __name__ == "__main__":
    main()
