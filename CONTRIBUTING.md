# Contributing

Reports from other laptop models are the most useful contribution. This sensor
ID appears in many machines, and the flash state differs between them.

Before opening an issue:

1. Confirm the sensor is `138a:0090` with `lsusb`.
2. Remove any other fingerprint stack. Only one driver can own the device.
3. Run `sudo ./scripts/repair.py` and keep the log.

When reporting a failure, include:

- Laptop model and distribution
- `lsusb | grep 138a` output
- `pacman -Q | grep -Ei 'fprint|validity'` output
- `journalctl -u fprintd -n 100 --no-pager`
- The repair log

Remove serial numbers and anything else you consider private first.

## Code changes

Keep changes small and test them on real hardware. The flash repair path is
destructive, so describe what you changed and the result of a full run.

The script targets the `0090` variant only. Do not generalize the flash
geometry or the partition layout without a successful test on the target
hardware.
