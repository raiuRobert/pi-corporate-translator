"""Windows companion for the Corporate Translator dongle.

Bridges the PC clipboard to the Pi over the USB CDC ACM serial port. Run this
in the background while the dongle is plugged in.

Protocol (newline-delimited, clipboard text is base64 UTF-8):
    Pi  -> here : GET_CLIP            -> reply CLIP:<base64 of current clipboard>
    Pi  -> here : SET_CLIP:<base64>   -> decode and put it on the clipboard
    here -> Pi : TRIGGER              -> start a translate flow (hotkey-driven)

Global hotkey (Ctrl+Shift+J -- intentionally no Alt, since pressing
Alt activates the menu bar in Notepad/Office and deselects your text):
- COMPANION_MODE=pc (default): the companion runs the full flow locally
  using SendInput keystroke synthesis and the PC's own Claude credentials.
  Works whether or not the Pi is connected.
- COMPANION_MODE=dongle: the companion sends a TRIGGER line over serial
  and the Pi runs the flow (HID Ctrl+C / Ctrl+V). Requires the Pi to be
  running ``main.py --trigger serial``.

Why two modes: HID delivery from a USB gadget can flake out across host
suspend/resume cycles; the pc mode is a zero-config fallback that gives
the same UX (~100 ms latency) without depending on the dongle.

The Pi's COM port is auto-detected by description and reconnected on disconnect.
"""

import base64
import ctypes
import ctypes.wintypes as wintypes
import os
import secrets
import sys
import threading
import time

# Make ../translator.py importable for the pc-mode flow
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                 os.pardir)))

import serial
import serial.tools.list_ports
import win32clipboard

from translator import translate, TranslationError

BAUD = 115200
PORT_HINTS = ("usb serial", "cdc", "acm", "corporate translator")
MODE = os.environ.get("COMPANION_MODE", "pc").lower()  # "pc" or "dongle"

# Ctrl+Shift+J as a Windows global hotkey. Avoids Alt because Alt
# activates the menu bar in Notepad/Office and deselects the user's text
# before our SendInput Ctrl+C can read it.
HOTKEY_ID = 1
MOD_SHIFT = 0x0004
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
HOTKEY_VK = 0x4A           # VK_J
HOTKEY_LABEL = "Ctrl+Shift+J"
VK_C = 0x43
VK_V = 0x56
VK_CTRL = 0x11
WM_HOTKEY = 0x0312


# --- SendInput keystroke synthesis (pc mode) ----------------------------
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT),
                    ("hi", _HARDWAREINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


_user32 = ctypes.windll.user32


def _send_key(vk: int, flags: int = 0) -> None:
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0,
                         dwExtraInfo=None)
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _send_chord(modifier_vk: int, key_vk: int) -> None:
    _send_key(modifier_vk)
    _send_key(key_vk)
    time.sleep(0.02)
    _send_key(key_vk, KEYEVENTF_KEYUP)
    _send_key(modifier_vk, KEYEVENTF_KEYUP)


def find_pi_port():
    """Return the device name of the Pi's serial port, or None."""
    for port in serial.tools.list_ports.comports():
        haystack = ((port.description or "") + " " + (port.product or "")).lower()
        if any(hint in haystack for hint in PORT_HINTS):
            return port.device
    return None


def get_clipboard_text() -> str:
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        return ""
    finally:
        win32clipboard.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def handle_line(ser: serial.Serial, line: str) -> None:
    if line == "GET_CLIP":
        text = get_clipboard_text() or ""
        payload = base64.b64encode(text.encode("utf-8"))
        ser.write(b"CLIP:" + payload + b"\n")
        ser.flush()
        print("-> sent clipboard (%d chars)" % len(text))
    elif line.startswith("SET_CLIP:"):
        payload = line[len("SET_CLIP:"):].encode("ascii", "ignore")
        try:
            text = base64.b64decode(payload).decode("utf-8")
        except Exception as exc:
            print("!! bad SET_CLIP payload: %s" % exc)
            return
        set_clipboard_text(text)
        print("<- clipboard set (%d chars)" % len(text))


# --- Hotkey -> TRIGGER ---------------------------------------------------
# The serial connection comes and goes; the hotkey thread sends through
# whichever Serial object is currently live (None when disconnected).
_SER_LOCK = threading.Lock()
_CURRENT_SER = {"ser": None}


def _set_current_ser(ser):
    with _SER_LOCK:
        _CURRENT_SER["ser"] = ser


_FLOW_LOCK = threading.Lock()  # prevents overlapping pc-mode flows


def _send_trigger_to_pi() -> bool:
    """Forward a TRIGGER to the Pi over serial. Returns True if delivered."""
    with _SER_LOCK:
        ser = _CURRENT_SER["ser"]
        if ser is None:
            return False
        try:
            ser.write(b"TRIGGER\n")
            ser.flush()
            print("-> TRIGGER (sent to Pi)")
            return True
        except Exception as exc:
            print("!! failed to send TRIGGER: %s" % exc)
            return False


def _pc_run_flow() -> None:
    """Run the whole copy->translate->paste cycle locally using SendInput."""
    if not _FLOW_LOCK.acquire(blocking=False):
        print("pc-flow already running; ignoring hotkey")
        return
    try:
        sentinel = "__CT_SENTINEL_%s__" % secrets.token_hex(8)
        set_clipboard_text(sentinel)
        time.sleep(0.08)
        _send_chord(VK_CTRL, VK_C)
        time.sleep(0.15)
        original = get_clipboard_text() or ""
        if original == sentinel or not original.strip():
            print("!! nothing selected -- aborting (clipboard unchanged)")
            return
        print("translating (%d chars): %r" % (len(original), original[:60]))
        try:
            rewritten = translate(original)
        except TranslationError as exc:
            print("!! translate failed: %s" % exc)
            return
        except Exception as exc:  # noqa: BLE001
            print("!! unexpected translate error: %s" % exc)
            return
        set_clipboard_text(rewritten)
        time.sleep(0.08)
        _send_chord(VK_CTRL, VK_V)
        print("OK: %r -> %r" % (original[:60], rewritten[:60]))
    finally:
        _FLOW_LOCK.release()


def _on_hotkey() -> None:
    if MODE == "dongle":
        if not _send_trigger_to_pi():
            print("hotkey pressed but dongle not connected (mode=dongle)")
    else:
        # pc mode (default): do the whole flow here, with no Pi involvement
        threading.Thread(target=_pc_run_flow, daemon=True).start()


def _hotkey_loop() -> None:
    """Register the global hotkey and dispatch each press through _on_hotkey."""
    user32 = ctypes.windll.user32
    mods = MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
    if not user32.RegisterHotKey(None, HOTKEY_ID, mods, HOTKEY_VK):
        print("!! could not register %s hotkey (errno %d); "
              "another app may already own it"
              % (HOTKEY_LABEL, ctypes.get_last_error()))
        return
    print("Hotkey registered: %s (mode=%s)" % (HOTKEY_LABEL, MODE))
    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                _on_hotkey()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)


def serve(port: str) -> None:
    print("Connecting to %s ..." % port)
    with serial.Serial(port, BAUD, timeout=0.2) as ser:
        print("Connected. Waiting for the dongle.")
        _set_current_ser(ser)
        try:
            buf = b""
            while True:
                chunk = ser.read(256)
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.strip().decode("utf-8", "ignore")
                        if line:
                            handle_line(ser, line)
        finally:
            _set_current_ser(None)


def main() -> None:
    threading.Thread(target=_hotkey_loop, daemon=True).start()
    while True:
        port = find_pi_port()
        if not port:
            print("Pi serial port not found; retrying in 2s ...")
            time.sleep(2)
            continue
        try:
            serve(port)
        except (serial.SerialException, OSError) as exc:
            print("Disconnected (%s); reconnecting in 2s ..." % exc)
            time.sleep(2)


if __name__ == "__main__":
    main()
