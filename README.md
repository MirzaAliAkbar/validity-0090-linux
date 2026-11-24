# Validity 138a:0090 fingerprint sensor on Linux

A working setup for the Validity/Synaptics `138a:0090` fingerprint sensor
found in many ThinkPads and other laptops, on Arch Linux and Omarchy. This
sensor has a long history of "no Linux support" reports. It works here with
fingerprint enrollment and `sudo`/polkit authentication through `fprintd`.

```
$ fprintd-list "$USER"
found 1 devices
Device at /net/reactivated/Fprint/Device/0
Using device /net/reactivated/Fprint/Device/0
User <you> has 1 finger enrolled.
```

## The short version

Two driver stacks exist for this sensor. Only one can enroll fingerprints.

| Stack | Package | Mode | Enroll |
| --- | --- | --- | --- |
| python-validity | `python-validity`, `open-fprintd` | Match-on-Chip | No |
| libfprint vfs0090 | `libfprint-vfs009x-git`, `fprintd` | Match-on-Host | Yes |

python-validity was the usual recommendation. It talks to the sensor, but the
matching happens inside the sensor, and the public protocol it exposes has no
enrollment command. That is why people conclude the sensor is unsupported.

The `libfprint-vfs009x-git` fork by 3v1n0 implements the vfs0090 driver in
libfprint. It runs matching on the host and supports `fprintd-enroll`.

The catch: the driver expects a sensor whose flash contains a valid partition
table, certificate store, and TLS material. Many `0090` sensors in the wild
have a blank or corrupted flash, or they were initialized by a different
stack. The driver then fails during init with status codes such as `0401`,
`0315`, `0406`, `04af`, or `044f`, and `fprintd` reports no devices.

`scripts/repair.py` fixes that. It rebuilds the sensor flash through the
driver's own initialization path, using the same commands the driver uses, so
the sensor ends up in the state the driver expects. It reads the partition
table first and only rebuilds when the table is missing, corrupt, or the
certificate store is unusable.

## Supported models

The `138a:0090` is the fingerprint reader used across the 2016 ThinkPad
generation. The system database calls it the `VFS7500 Touch Fingerprint
Sensor`. It is a press-type sensor with five enrollment stages. The same USB
ID appears in several models, so the fix works the same way on each of them.

| Model | Status |
| --- | --- |
| ThinkPad T460s (`20FAS5PA00`) | Confirmed working. This repo was built on it. |
| ThinkPad T460, T460p, T560 | Expected to work. Same sensor generation. |
| ThinkPad X260, X1 Carbon 4th Gen | Expected to work. Same sensor generation. |
| ThinkPad P50s, L460, L560, Yoga 460 | Expected to work. Same sensor generation. |
| Any other laptop with `138a:0090` | Try it, then open a report. |

"Expected" means the machine ships the same sensor and the same driver path,
but nobody has posted a log from it here yet. Confirm your own machine before
you rely on the table:

```bash
lsusb | grep 138a
```

You want `138a:0090`. A different product ID such as `138a:0097` uses the same
driver family but a different flash layout, so the repair script does not
handle it.

If your model is missing, run the setup and open a
[sensor report](.github/ISSUE_TEMPLATE/sensor-report.yml). The template asks
for the model, the distro, and the log. Reports are how the table gets filled
in.

## Requirements

- Arch Linux or an Arch-based distribution such as Omarchy
- An AUR helper, `yay` in the scripts
- The `138a:0090` sensor (`lsusb` shows `Validity Sensors, Inc. ...`)
- Secure Boot disabled, or the kernel module allowed, if your setup blocks
  unsigned AUR kernel modules. `libfprint-vfs009x-git` is a userspace library,
  so this normally does not apply.

Check the sensor is present:

```
lsusb | grep 138a
```

## Install

```bash
git clone https://github.com/MirzaAliAkbar/validity-0090-linux.git
cd validity-0090-linux
./scripts/install-driver.sh
```

This stops and disables the python-validity daemons, removes the conflicting
`libfprint-git` and `fprintd-clients-git` packages, installs
`libfprint-vfs009x-git` and `fprintd`, installs the udev rule that keeps the
sensor powered, and enables `fprintd`. `python-validity` itself can stay
installed. Only one daemon may claim the sensor at a time.

Then check:

```bash
fprintd-list "$USER"
```

If the device is listed, enroll:

```bash
fprintd-enroll
```

If it says `No devices found`, the flash needs repair. Continue below.

## Repair a blank or corrupt sensor

Run the repair script. It re-executes itself with `sudo` and asks for your
password once.

```bash
sudo ./scripts/repair.py
```

The script does this:

1. Stops and masks the driver stack and clears the restart backoff.
2. Power-cycles the sensor's USB port.
3. Reads the partition table with command `0x3e` in a separate process.
4. If the table is valid, starts the driver and waits for `fprintd`. If the
   driver fails with a certificate or TLS status, it continues to the rebuild.
5. Rebuilds the flash through the driver's own `init_flash()` and
   `partition_flash()` path, verifies the new table, and starts the driver.

It writes a log to `/var/log/validity-0090-repair.log`. Set `FP_LOG` to put it
somewhere else.

```
17:44:45   3e -> 0000 (len 148)
17:44:45     id=1   type=4   access=7     off=0x001000 size=0x001000
17:44:45     id=2   type=1   access=2     off=0x002000 size=0x03e000
17:44:45     id=5   type=5   access=3     off=0x040000 size=0x008000
17:44:45     id=6   type=6   access=3     off=0x048000 size=0x008000
17:44:45     id=4   type=3   access=5     off=0x050000 size=0x030000
17:44:45   layout matches 0090: True
17:46:44   driver did not come up (last status: 0406)
17:46:44   certificate/TLS store is unusable (status 0406); rebuilding the flash
17:46:57 FORMAT_OK: sensor rebooted after the rebuild
17:47:13 RESULT: sensor rebuilt and driver is up; enrollment is available
```

The full log from the run on this machine is in
[`docs/example-repair.log`](docs/example-repair.log).

After repair, enroll:

```bash
fprintd-enroll
fprintd-verify
```

## Recover after suspend or a bad re-enumeration

A soft USB re-enumeration can leave a stale TLS session on the sensor. The
driver then fails with a timeout or `No devices found`. Power-cycle the port:

```bash
sudo ./scripts/recover.sh
```

This is safe to run at any time. It does not touch the flash.

## Enable fingerprint login

`fprintd` is only the daemon. PAM decides where fingerprints are accepted.
On Omarchy, the setup helper does this:

```bash
omarchy-setup-security-fingerprint
```

The helper in older Omarchy releases installs `libfprint-git`, which has no
vfs0090 driver and replaces the working package. `scripts/install-driver.sh`
does not patch the helper. If you run the helper after this setup, check the
package list first. `libfprint-vfs009x-git` and `libfprint-git` conflict.

For a manual PAM setup, add this line to `/etc/pam.d/sudo` and
`/etc/pam.d/polkit-1`:

```
auth      sufficient pam_fprintd.so
```

Put it above the other `auth` lines. Keep a password fallback available. Test
in a spare TTY before relying on it for login.

## How the flash repair works

The sensor exposes a small command protocol over USB. Command `0x3e` returns
the partition table. A valid `0090` table has five partitions:

| ID | Type | Access | Offset | Size |
| --- | --- | --- | --- | --- |
| 1 | 4 | 7 | `0x001000` | `0x001000` |
| 2 | 1 | 2 | `0x002000` | `0x03e000` |
| 5 | 5 | 3 | `0x040000` | `0x008000` |
| 6 | 6 | 3 | `0x048000` | `0x008000` |
| 4 | 3 | 5 | `0x050000` | `0x030000` |

The flash is a Winbond W25Q80B: 4096 blocks of 256 bytes, 1 MiB total.

The driver's `init_flash()` only formats when `0x3e` returns status `0000`
with zero partitions. It raises on `0401` and similar statuses instead of
repairing. The script patches `get_flash_info()` to route those statuses into
the driver's own rebuild path, then calls `init.open()`. That path writes a new
partition table, certificate store, and TLS material, then reboots the sensor.

Two details matter for this sensor variant:

- The `0090` partition table is unsigned. The signed path is rejected with
  `0404`, so the script clears `partition_signature_0090`.
- USB data toggle can break after a re-enumeration. The script patches
  `Usb.open_dev` to skip `set_configuration()` when configuration 1 is already
  active.

A factory reset clears the old table before the rebuild. The script retries
the reset up to three times and cold power-cycles the port between attempts,
because the table can persist until the port loses power.

## Notes and limits

- The flash rebuild erases the sensor's stored state. Fingerprints enrolled
  with another stack are gone. Re-enroll after repair.
- The repair path is specific to the `0090` variant and its flash geometry.
  Do not run it against another Validity sensor.
- `scripts/repair.py` drives the sensor through `validitysensor`, the Python
  module from python-validity. This repo keeps a copy in
  `vendor/validitysensor` so the script works without that package. It is
  vendored at version 0.15 under its MIT license.
- The USB port lookup assumes the usual `/sys/bus/usb/devices/<bus>-0:1.0/`
  layout. It works on the ThinkPad where this was developed. If your machine
  differs, `recover.sh` prints the path it tries.
- Enrollment quality depends on the sensor and finger placement. If
  enrollment fails partway, run `recover.sh` and try again with a dry finger.

## Credits

- [3v1n0/libfprint](https://github.com/3v1n0/libfprint) for the vfs0090 driver.
- The [python-validity](https://github.com/uunicorn/python-validity) project
  for documenting the sensor protocol.
- The `validitysensor` module in `vendor/` comes from python-validity and is
  used under its MIT license.
- Everyone who filed the issue reports that mapped the status codes.

## License

MIT. See [LICENSE](LICENSE).
