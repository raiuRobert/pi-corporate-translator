"""Windows companion for the Corporate Translator dongle.

Bridges the PC clipboard to the Pi over the USB CDC ACM serial port. Run this
in the background while the dongle is plugged in.

Protocol (newline-delimited, clipboard text is base64 UTF-8):
    Pi  -> here : GET_CLIP            -> reply CLIP:<base64 of current clipboard>
    Pi  -> here : SET_CLIP:<base64>   -> decode and put it on the clipboard
    here -> Pi : TRIGGER              -> start a translate flow (hotkey-driven)

When the user presses the global hotkey (Ctrl+Alt+T), this script sends
TRIGGER over the serial line so the Pi's --trigger serial mode starts a
cycle without the user ever having to leave their target window. Because
the hotkey fires from the focused window, focus is naturally preserved
through the (~3 s) round-trip back to Ctrl+V.

The Pi's COM port is auto-detected by description and reconnected on disconnect.
"""

import base64
import ctypes
import ctypes.wintypes as wintypes
import threading
import time

import serial
import serial.tools.list_ports
import win32clipboard

BAUD = 115200
PORT_HINTS = ("usb serial", "cdc", "acm", "corporate translator")

# Ctrl+Alt+T as a Windows global hotkey
HOTKEY_ID = 1
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_T = 0x54
WM_HOTKEY = 0x0312


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


def _send_trigger() -> None:
    with _SER_LOCK:
        ser = _CURRENT_SER["ser"]
        if ser is None:
            print("hotkey pressed but dongle not connected")
            return
        try:
            ser.write(b"TRIGGER\n")
            ser.flush()
            print("-> TRIGGER (hotkey)")
        except Exception as exc:
            print("!! failed to send TRIGGER: %s" % exc)


def _hotkey_loop() -> None:
    """Register Ctrl+Alt+T and forward each press as a TRIGGER over serial."""
    user32 = ctypes.windll.user32
    if not user32.RegisterHotKey(None, HOTKEY_ID,
                                 MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_T):
        print("!! could not register Ctrl+Alt+T hotkey (errno %d); "
              "another app may already own it" % ctypes.get_last_error())
        return
    print("Hotkey registered: Ctrl+Alt+T")
    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                _send_trigger()
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
