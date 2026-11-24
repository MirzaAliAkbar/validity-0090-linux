# Troubleshooting

## `No devices found`

`fprintd` cannot see the sensor. Check the layers in order.

```bash
lsusb | grep 138a
```

No output means the USB device is missing. Check the physical connection and
the kernel log:

```bash
sudo dmesg | tail -40
```

If `lsusb` shows the device but `fprintd-list` does not, the driver failed to
open it. Check the journal:

```bash
journalctl -u fprintd -n 50 --no-pager
```

A `transfer timed out`, `Device ... has not been opened`, or a status code in
the `04xx` range means the flash repair path is needed:

```bash
sudo ./scripts/repair.py
```

A stale TLS session gives the same symptom after a suspend. Try the cheap fix
first:

```bash
sudo ./scripts/recover.sh
```

## Status codes

These come from the driver's initialization. The script logs them as
`Failed: XXXX`.

| Status | Meaning | Action |
| --- | --- | --- |
| `0000` | Success | None |
| `0401` | No usable partition table | Repair |
| `0315` | Partition or cert store invalid | Repair |
| `0406` | Certificate/TLS store unusable | Repair |
| `04af` | TLS session not ready | Expected on a plaintext read; repair only if it persists during init |
| `044f` | TLS or certificate failure | Repair |
| `0404` | Signed partition table rejected | The `0090` table must be unsigned; the script handles this |

## `Resource busy` / `Errno 16`

The sensor's USB handle was left open by another process. Stop the stacks and
retry. The repair script does this itself:

```bash
sudo systemctl stop fprintd.service python3-validity.service open-fprintd.service
```

If the handler is a stale process, find it:

```bash
sudo fuser -v /dev/bus/usb/*/*
```

## Enrollment stops after the first touch

The sensor autosuspends between touches and the second sample fails. Check the
udev rule is installed and the device is powered on:

```bash
grep 138a /etc/udev/rules.d/61-validity-no-autosuspend.rules
cat /sys/bus/usb/devices/<device>/power/control
```

`power/control` must be `on`. Reinstall the rule with
`./scripts/install-driver.sh`, then replug the sensor or run
`sudo ./scripts/recover.sh`.

## Conflict between `libfprint-git` and `libfprint-vfs009x-git`

They provide the same library. Only one may be installed. The standard
`libfprint-git` has no vfs0090 driver. If a system helper reinstalls it, remove
it and reinstall the fork:

```bash
sudo pacman -Rdd --noconfirm libfprint-git
yay -S --needed --noconfirm libfprint-vfs009x-git
sudo systemctl restart fprintd
```

## Repair says the table is intact but the driver cannot open a session

The flash is valid but something else blocks the session. The script stops
before writing in this case because the data is good. Check:

- Another fingerprint daemon is running (`pgrep -af fprintd`).
- The device is authorized: `cat /sys/bus/usb/devices/<device>/authorized`.
- No VM or remote session holds the USB device.
- The kernel did not reset the port: `sudo dmesg | grep -i usb | tail -30`.

Report the journal output in an issue if none of these apply.

## Repair fails and leaves the sensor without a table

The script only writes after it has read a table or confirmed the table is
missing. If the rebuild is interrupted, the flash can be left blank. Run the
repair again. It is designed to recover from that state, because a blank table
is one of the cases it handles.

Do not power off the machine during the rebuild. The erase and write take a few
seconds, and the sensor reboots at the end.

## Collecting a report

Run these and attach the output to an issue:

```bash
lsusb | grep -i validity
pacman -Q | grep -Ei 'fprint|validity'
journalctl -u fprintd -n 100 --no-pager
sudo ./scripts/repair.py
```

The repair log at `/var/log/validity-0090-repair.log` has the partition table
and every USB command. Remove anything you consider private before posting.
