#!/usr/bin/env bash
# Power-cycle the 138a:0090 sensor's USB port, then restart fprintd.
#
# Use this when fprintd reports "No devices found" or the driver fails after
# a resume, a suspend, or a udev re-enumeration. The sensor keeps a stale TLS
# session across a soft re-enumeration, and a port power cycle clears it.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$0" "$@"
fi

found=0
for d in /sys/bus/usb/devices/*/idVendor; do
    [ "$(cat "$d" 2>/dev/null)" = "138a" ] || continue
    devdir="$(dirname "$d")"
    [ "$(cat "$devdir/idProduct" 2>/dev/null)" = "0090" ] || continue

    found=1
    name="$(basename "$devdir")"
    bus="${name%%-*}"
    port="${name#*-}"
    portdir="/sys/bus/usb/devices/${bus}-0:1.0/usb${bus}-port${port}"

    if [ -d "$portdir" ]; then
        echo "Power-cycling $portdir"
        echo 1 > "$portdir/disable"
        sleep 2
        echo 0 > "$portdir/disable"
        sleep 4
    else
        echo "Port controller $portdir not found, resetting the device instead"
        echo 1 > "$devdir/authorized" 2>/dev/null || true
    fi
done

if [ "$found" -eq 0 ]; then
    echo "No 138a:0090 sensor found on the USB bus." >&2
    exit 1
fi

# Keep the sensor powered once it is back.
for d in /sys/bus/usb/devices/*/idVendor; do
    [ "$(cat "$d" 2>/dev/null)" = "138a" ] || continue
    devdir="$(dirname "$d")"
    [ "$(cat "$devdir/idProduct" 2>/dev/null)" = "0090" ] && \
        echo on > "$devdir/power/control" 2>/dev/null || true
done

echo "Restarting fprintd"
systemctl restart fprintd.service
sleep 2

echo "Device list:"
if [ -n "${SUDO_USER:-}" ]; then
    fprintd-list "$SUDO_USER" || true
else
    fprintd-list root || true
fi
