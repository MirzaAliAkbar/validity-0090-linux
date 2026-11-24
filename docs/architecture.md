# How the stack fits together

```
application
    |
    v
PAM (pam_fprintd.so)
    |
    v
fprintd  (D-Bus daemon)
    |
    v
libfprint  (libfprint-vfs009x-git)
    |
    v
vfs0090 driver
    |
    v
USB 138a:0090
```

## The two stacks

`python-validity` is a userspace implementation of the sensor protocol. It
runs the sensor in Match-on-Chip mode: the sensor stores and compares
templates internally. Its D-Bus surface exposes verification but not
enrollment, so common desktop tools cannot add a fingerprint.

`libfprint-vfs009x-git` is a fork of libfprint with the vfs0090 driver. It
uses Match-on-Host: the host receives fingerprint images and stores templates
in libfprint's own storage. `fprintd` then exposes enroll, verify, list, and
delete through D-Bus, and `pam_fprintd` uses that for authentication.

Only one daemon can own the USB device at a time. Running both produces
`Resource busy` errors, so the install script disables the python-validity
services and leaves only `fprintd` enabled.

## Why the flash matters

The driver's init sequence reads a partition table, opens a TLS session using
certificates stored in flash, and then talks to the sensor over that session.
If the flash is blank or the certificate store is invalid, init fails before a
session exists. The failure is reported as a status code, not as a missing
device, which is why the symptom looks like "no driver" rather than "bad
flash".

A factory state sensor or a sensor previously owned by another stack can be in
this condition. The repair script writes the expected structure using the
driver's own code path, so the layout and certificates match what the driver
later expects.

## What the repair changes

The script does not reimplement the flash protocol. It imports
`validitysensor`, the Python module from python-validity, and patches three
behaviors:

1. `get_flash_info()` treats "no table" statuses as uninitialized instead of
   raising, so `init.open()` takes the rebuild path.
2. `partition_signature_0090` is empty, because the `0090` table is unsigned.
3. `Usb.open_dev()` skips `set_configuration()` when configuration 1 is
   already active, avoiding a USB data toggle failure after re-enumeration.

Everything else, including erase, write, and the TLS material, runs through
that module's functions.

The flash helpers live in python-validity, while the working driver is
`libfprint-vfs009x-git`. The packages can coexist, but their daemons cannot
both run. This repo vendors the helpers in `vendor/` at version 0.15 under the
MIT license so repair works whether or not python-validity is installed. The C
driver and the Python helpers speak the same protocol, and the flash layout in
the module matches what the driver expects.

## Processes

The script runs the destructive rebuild in a child process. A libusb handle
left open by a read-only probe makes the rebuild fail with `Resource busy`, so
probing, formatting, and verification each get a fresh process.
