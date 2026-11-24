#!/usr/bin/env bash
# Install the libfprint vfs0090 driver and fprintd.
#
# python-validity runs the sensor in Match-on-Chip mode and cannot enroll
# fingerprints. The libfprint vfs0090 fork runs Match-on-Host and supports
# fprintd-enroll. Both daemons claim 138a:0090, so only one may run at a time.
# python-validity itself can stay installed for its validitysensor module.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user. It calls sudo where needed." >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

echo "=== [1/6] Stopping python-validity stack ==="
sudo systemctl stop python3-validity.service open-fprintd.service 2>/dev/null || true
sudo systemctl disable python3-validity.service open-fprintd.service 2>/dev/null || true

echo "=== [2/6] Removing conflicting packages ==="
# libfprint-git and libfprint conflict with the vfs009x fork and lack the
# vfs0090 driver. fprintd-clients-git conflicts with fprintd.
for pkg in libfprint-git libfprint fprintd-clients-git; do
    sudo pacman -Rdd --noconfirm "$pkg" 2>/dev/null || true
done

# python-validity can stay installed. Its daemons are disabled above, and
# scripts/repair.py uses its Python module if the vendored copy is missing.
if pacman -Q python-validity >/dev/null 2>&1; then
    echo "python-validity is installed; its services stay disabled."
fi

echo "=== [3/6] Installing libfprint-vfs009x-git and fprintd ==="
if ! command -v yay >/dev/null 2>&1; then
    echo "yay is required for the AUR package. Install yay first." >&2
    exit 1
fi
yay -S --needed --noconfirm libfprint-vfs009x-git
sudo pacman -S --needed --noconfirm --ask 4 -- fprintd usbutils

echo "=== [4/6] Installing the no-autosuspend udev rule ==="
sudo install -Dm644 "$ROOT/udev/61-validity-no-autosuspend.rules" \
    /etc/udev/rules.d/61-validity-no-autosuspend.rules
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "=== [5/6] Enabling fprintd ==="
sudo systemctl enable --now fprintd.service

echo "=== [6/6] Checking for the sensor ==="
sleep 2
fprintd-list "$USER" || true

echo
echo "Driver installed. If the sensor is listed, enroll with: fprintd-enroll"
echo "If it says 'No devices found', run: sudo ./scripts/recover.sh"
