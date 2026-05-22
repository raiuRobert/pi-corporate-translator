"""USB HID keyboard helper.

Writes 8-byte boot-protocol keyboard reports to the gadget device created by
``setup_gadget.sh`` (default ``/dev/hidg0``). Only the two chords this project
needs are implemented: Ctrl+C and Ctrl+V.

HID report layout (boot keyboard): [modifiers, reserved, key1..key6].
  modifier 0x01 = Left Ctrl
  usage 0x06    = 'c'
  usage 0x19    = 'v'
"""

import time

HID_DEVICE = "/dev/hidg0"

# Pre-built reports.
_CTRL_C = bytes([0x01, 0x00, 0x06, 0x00, 0x00, 0x00, 0x00, 0x00])
_CTRL_V = bytes([0x01, 0x00, 0x19, 0x00, 0x00, 0x00, 0x00, 0x00])
_RELEASE = bytes(8)

# Delay between press and release so the host registers the chord.
_PRESS_HOLD_S = 0.02


def _send_report(report: bytes, device: str = HID_DEVICE) -> None:
    with open(device, "wb") as fh:
        fh.write(report)
        fh.flush()


def _send_chord(chord: bytes, device: str = HID_DEVICE) -> None:
    try:
        _send_report(chord, device)
        time.sleep(_PRESS_HOLD_S)
    finally:
        # Always release, even if the press path raised -- otherwise the host
        # sees the chord held down and auto-repeats it (e.g. Ctrl+C forever).
        _send_report(_RELEASE, device)
        time.sleep(_PRESS_HOLD_S)


def send_copy(device: str = HID_DEVICE) -> None:
    """Press Ctrl+C on the attached host."""
    _send_chord(_CTRL_C, device)


def send_paste(device: str = HID_DEVICE) -> None:
    """Press Ctrl+V on the attached host."""
    _send_chord(_CTRL_V, device)


def release_all(device: str = HID_DEVICE) -> None:
    """Send an all-keys-released report to clear any stuck key on the host.

    Safe to call at any time; failures are swallowed so it can be used as a
    best-effort cleanup on startup and shutdown.
    """
    try:
        _send_report(_RELEASE, device)
    except Exception:
        pass


if __name__ == "__main__":
    # Manual smoke test: copy, then paste.
    send_copy()
    time.sleep(0.2)
    send_paste()
