"""Windows companion for the Corporate Translator dongle.

Bridges the PC clipboard to the Pi over the USB CDC ACM serial port. Run this
in the background while the dongle is plugged in.

Protocol (newline-delimited, clipboard text is base64 UTF-8):
    Pi  -> here : GET_CLIP            -> reply CLIP:<base64 of current clipboard>
    Pi  -> here : SET_CLIP:<base64>   -> decode and put it on the clipboard

The Pi's COM port is auto-detected by description and reconnected on disconnect.
"""

import base64
import time

import serial
import serial.tools.list_ports
import win32clipboard

BAUD = 115200
PORT_HINTS = ("usb serial", "cdc", "acm", "corporate translator")


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


def serve(port: str) -> None:
    print("Connecting to %s ..." % port)
    with serial.Serial(port, BAUD, timeout=0.2) as ser:
        print("Connected. Waiting for the dongle.")
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


def main() -> None:
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
