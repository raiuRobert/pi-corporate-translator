#!/bin/bash
# Configure a USB composite gadget (HID keyboard + CDC ACM serial) on the
# Raspberry Pi Zero 2W using libcomposite / configfs.
#
# Result on the host (Windows 10/11, no driver install needed):
#   - a standard USB HID boot keyboard  -> /dev/hidg0 on the Pi
#   - a USB CDC ACM serial port         -> /dev/ttyGS0 on the Pi
#
# Idempotent: any existing gadget of the same name is torn down first.
# Must run as root, after the dwc2 and libcomposite modules are loaded.
set -euo pipefail

GADGET_NAME="corp_translator"
GADGET_DIR="/sys/kernel/config/usb_gadget/${GADGET_NAME}"

modprobe libcomposite

teardown() {
    [ -d "${GADGET_DIR}" ] || return 0
    # Detach from the UDC first, then unlink and remove in reverse order.
    if [ -e "${GADGET_DIR}/UDC" ] && [ -s "${GADGET_DIR}/UDC" ]; then
        echo "" > "${GADGET_DIR}/UDC" || true
    fi
    for cfg in "${GADGET_DIR}"/configs/*; do
        [ -d "${cfg}" ] || continue
        for link in "${cfg}"/*; do
            [ -L "${link}" ] && rm -f "${link}"
        done
        for s in "${cfg}"/strings/*; do
            [ -d "${s}" ] && rmdir "${s}"
        done
        rmdir "${cfg}"
    done
    for func in "${GADGET_DIR}"/functions/*; do
        [ -d "${func}" ] && rmdir "${func}"
    done
    for s in "${GADGET_DIR}"/strings/*; do
        [ -d "${s}" ] && rmdir "${s}"
    done
    rmdir "${GADGET_DIR}"
}

teardown

mkdir -p "${GADGET_DIR}"
cd "${GADGET_DIR}"

# --- Device descriptor ---------------------------------------------------
echo 0x1d6b > idVendor          # Linux Foundation
echo 0x0104 > idProduct         # Multifunction Composite Gadget
echo 0x0100 > bcdDevice         # v1.0.0
echo 0x0200 > bcdUSB            # USB 2.0

mkdir -p strings/0x409
echo "fedcba9876543210" > strings/0x409/serialnumber
echo "raiuRobert"       > strings/0x409/manufacturer
echo "Corporate Translator" > strings/0x409/product

# --- Configuration -------------------------------------------------------
mkdir -p configs/c.1/strings/0x409
echo "HID keyboard + CDC ACM" > configs/c.1/strings/0x409/configuration
echo 250 > configs/c.1/MaxPower

# --- Function: HID boot keyboard ----------------------------------------
mkdir -p functions/hid.usb0
echo 1 > functions/hid.usb0/protocol      # 1 = keyboard
echo 1 > functions/hid.usb0/subclass      # 1 = boot interface
echo 8 > functions/hid.usb0/report_length # 8-byte reports
# Standard boot-keyboard HID report descriptor.
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' > functions/hid.usb0/report_desc

# --- Function: CDC ACM serial -------------------------------------------
mkdir -p functions/acm.usb0

# --- Link functions into the configuration ------------------------------
ln -s functions/hid.usb0 configs/c.1/
ln -s functions/acm.usb0 configs/c.1/

# --- Activate: bind to the first available UDC --------------------------
UDC_DEV="$(ls /sys/class/udc | head -n1)"
echo "${UDC_DEV}" > UDC

echo "USB gadget '${GADGET_NAME}' bound to ${UDC_DEV} (HID + ACM)."
