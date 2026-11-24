#!/usr/bin/env python3
"""Repair the flash of a Validity 138a:0090 fingerprint sensor.

Run:  python3 scripts/repair.py

The script re-executes itself with sudo.  USB work is split into short-lived
child processes: a read-only probe, a destructive rebuild, and a verify pass.
Keeping them in separate processes matters because a libusb handle left open
by the probe makes the rebuild's open() fail with USBError(16, 'Resource
busy').

Flow:
  1. Stop and mask the driver stack and clear the restart backoff.
  2. Power-cycle the sensor's USB port.
  3. Probe the partition table in a fresh process (plaintext 0x3e).
  4. If the table is valid, start the driver and wait for fprintd.  If the
     driver then fails with a TLS/certificate status (04af, 0401, 0315, 0406,
     044f) the certificate store is unusable and the flash is rebuilt.
  5. Rebuild the flash in a second fresh process through the driver's own
     init_flash()/partition_flash() path, verify, and start the driver.

Status 04af on a plaintext read is expected before TLS is up.  It only means
"rebuild" when the driver keeps reporting it during initialization.
"""

import glob
import json
import os
import re
import subprocess
import sys
import time
from struct import unpack

LOG = os.environ.get('FP_LOG', '/var/log/validity-0090-repair.log')
VID, PID = 0x138a, 0x0090

# validitysensor ships with python-validity. This project keeps a copy in
# vendor/ so the script works whether or not that package is installed.
VENDOR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vendor')


def target_user():
    """The desktop user, even after the script re-execs itself with sudo."""
    return os.environ.get('SUDO_USER') or os.environ.get('USER') or 'root'

# Flash geometry reported by this sensor: Winbond W25Q80B.
FLASH_JEDEC = (0xef, 0x40)
FLASH_BLOCKS = 4096
FLASH_BLOCKSIZE = 256
FLASH_SIZE = FLASH_BLOCKS * FLASH_BLOCKSIZE

# Layout for the 0090 variant, from validitysensor/init_flash.py.
EXPECTED = [
    (1, 4, 7, 0x001000, 0x001000),
    (2, 1, 2, 0x002000, 0x03e000),
    (5, 5, 3, 0x040000, 0x008000),
    (6, 6, 3, 0x048000, 0x008000),
    (4, 3, 5, 0x050000, 0x030000),
]

# Driver init statuses that mean the flash/certificate store needs rebuilding.
FORMAT_CODES = {0x0401, 0x0315, 0x0406, 0x04af, 0x044f}

RULE = '/etc/udev/rules.d/61-validity-no-autosuspend.rules'
RULE_TEXT = (
    '# Keep the Validity 138a:0090 sensor powered while the driver initializes.\n'
    'ACTION=="add|change", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", '
    'ATTR{idVendor}=="138a", ATTR{idProduct}=="0090", ATTR{power/control}="on"\n'
)


def log(*parts):
    line = '%s %s' % (time.strftime('%H:%M:%S'), ' '.join(str(p) for p in parts))
    print(line, flush=True)
    try:
        with open(LOG, 'a') as fh:
            fh.write(line + '\n')
    except Exception:
        pass


def rd(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception as exc:
        return 'ERR(%s)' % exc


def wr(path, value):
    try:
        with open(path, 'w') as fh:
            fh.write(str(value))
        return True
    except Exception as exc:
        log('  write %s=%s failed: %s' % (path, value, exc))
        return False


def sh(*args, timeout=180):
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (res.stdout + res.stderr).strip()
    except Exception as exc:
        return 'EXC %s' % exc


def find_dev():
    for path in sorted(glob.glob('/sys/bus/usb/devices/*')):
        if (rd(path + '/idVendor') == '%04x' % VID
                and rd(path + '/idProduct') == '%04x' % PID):
            return path
    return None


def port_dir():
    dev = find_dev()
    if dev:
        name = os.path.basename(dev)
        if '-' in name:
            bus, port = name.split('-', 1)
            cand = '/sys/bus/usb/devices/%s-0:1.0/usb%s-port%s' % (bus, bus, port)
            if os.path.isdir(cand):
                return cand
    for path in glob.glob('/sys/bus/usb/devices/*/usb*-port*'):
        if os.path.isdir(path):
            return path
    return None


def wait_dev(seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        path = find_dev()
        if path:
            return path
        time.sleep(0.5)
    return None


def release(usb):
    """Close the libusb handle without resetting the sensor."""
    dev = usb.dev
    usb.dev = None
    if dev is None:
        return
    try:
        import usb.util as usbutil
        usbutil.dispose_resources(dev)
    except Exception:
        pass
    try:
        dev.finalize()
    except Exception:
        pass


def open_usb(usb, retries=10, delay=1.0):
    last = None
    for _ in range(retries):
        try:
            usb.open()
            usb.trace_enabled = False
            return None
        except Exception as exc:
            last = exc
            release(usb)
            time.sleep(delay)
    return last


def parse_table(rsp):
    body = rsp[2:]
    jid0, jid1, blocks, unk0, bsize, unk1, pcnt = unpack('<HHHHHHH', body[:0xe])
    rest = body[0xe:]
    parts = []
    for i in range(pcnt):
        chunk = rest[i * 0xc:(i + 1) * 0xc]
        if len(chunk) < 0xc:
            break
        parts.append(unpack('<BBHLL', chunk))
    return jid0, jid1, blocks, unk0, bsize, unk1, parts


def layout_ok(parts):
    """A formatted 0090 returns five real records plus signature slices."""
    return parts[:len(EXPECTED)] == EXPECTED


# --------------------------------------------------------------------------
# child stages
# --------------------------------------------------------------------------

def stage_probe():
    """Read 0x3e once and print one JSON line.  Never writes to flash."""
    sys.path.insert(0, VENDOR)
    from validitysensor.usb import usb
    from validitysensor.util import unhex

    out = {'ok': False}
    try:
        err = open_usb(usb, retries=6, delay=1.0)
        if err is not None:
            raise err
        try:
            rsp = bytes(usb.cmd(unhex('3e')))
        finally:
            release(usb)
        st = unpack('<H', rsp[:2])[0]
        out.update(status='%04x' % st, length=len(rsp))
        if st == 0:
            parts = parse_table(rsp)[6]
            out['parts'] = [list(p) for p in parts]
            out['layout_ok'] = layout_ok(parts)
            out['ok'] = True
    except Exception as exc:
        out['error'] = repr(exc)
    print(json.dumps(out), flush=True)
    os._exit(0)


def stage_format():
    """Force the driver's own format path and exit before any atexit reset."""
    import logging
    logging.basicConfig(level=logging.DEBUG, format='%(levelname)s:%(name)s:%(message)s')

    sys.path.insert(0, VENDOR)
    from validitysensor import init, init_flash
    from validitysensor.flash import FlashInfo
    from validitysensor.hw_tables import flash_ic_table_lookup
    from validitysensor.sensor import RebootException
    from validitysensor.tls import tls
    from validitysensor.usb import usb
    from validitysensor.util import unhex

    state = {'partitioned': False}

    # Retry the device open across the re-enumeration window after a power cycle.
    orig_usb_open = usb.open

    def retrying_open(*a, **k):
        last = None
        for attempt in range(8):
            try:
                orig_usb_open(*a, **k)
                usb.trace_enabled = False
                return None
            except Exception as exc:
                last = exc
                log('  usb.open retry %d: %r' % (attempt + 1, exc))
                release(usb)
                time.sleep(1.5)
        raise last

    usb.open = retrying_open

    # Log every command/status pair so a failure is unambiguous.
    orig_cmd = usb.cmd

    def traced_cmd(out):
        if callable(out):
            return orig_cmd(out)
        try:
            rsp = orig_cmd(out)
        except Exception as exc:
            log('  cmd %s -> EXC %r' % (bytes(out[:2]).hex(), exc))
            raise
        head = bytes(rsp[:2]).hex() if len(rsp) >= 2 else '??'
        log('  cmd %s -> %s (%d B)' % (bytes(out[:2]).hex(), head, len(rsp)))
        return rsp

    usb.cmd = traced_cmd

    def wrap(name, marker=None):
        fn = getattr(init_flash, name)

        def inner(*a, **k):
            shown = ', '.join(str(x) for x in a if not isinstance(x, (bytes, bytearray)))
            log('  %s(%s)' % (name, shown[:120]))
            res = fn(*a, **k)
            if marker:
                state[marker] = True
            return res

        setattr(init_flash, name, inner)

    # wrap the flash operations that init_flash calls
    for name in ('partition_flash', 'erase_flash', 'write_flash', 'call_cleanups'):
        # these exist either in init_flash (imported) or in flash module
        if not hasattr(init_flash, name):
            import validitysensor.flash as _f
            setattr(init_flash, name, getattr(_f, name))
        wrap(name, 'partitioned' if name == 'partition_flash' else None)

    # write_fw_signature is only in flash module; ensure it exists on init_flash for any caller
    if not hasattr(init_flash, 'write_fw_signature'):
        import validitysensor.flash as _f
        setattr(init_flash, 'write_fw_signature', getattr(_f, 'write_fw_signature'))

    orig_tls_open = tls.open

    def traced_tls_open(*a, **k):
        log('  tls.open()')
        res = orig_tls_open(*a, **k)
        log('  tls.open ok')
        return res

    tls.open = traced_tls_open

    ic = flash_ic_table_lookup(FLASH_JEDEC[0], FLASH_JEDEC[1], FLASH_SIZE)
    if ic is None:
        log('FORMAT_FAIL: flash IC table lookup failed')
        sys.stdout.flush()
        os._exit(1)

    def patched_get_flash_info():
        tls.reset()
        try:
            rsp = tls.cmd(unhex('3e'))
            log('  pre-format 3e -> %s' % bytes(rsp[:2]).hex())
        except Exception as exc:
            log('  pre-format 3e failed: %r' % exc)
        log('  forcing init_flash() down the rebuild path')
        return FlashInfo(ic, FLASH_BLOCKS, 1, FLASH_BLOCKSIZE, 1, [])

    init_flash.get_flash_info = patched_get_flash_info
    # 0090 uses an unsigned partition table (PR #294); signed one is rejected (0404).
    try:
        init_flash.partition_signature_0090 = b''
    except Exception:
        pass
    tls.reset()

    # ---- PR #294 fixes ----
    # (a) USB data-toggle fix: skip set_configuration() when config 1 is
    # already active.
    orig_open_dev = usb.open_dev
    def fixed_open_dev(dev):
        import usb.core as ucore
        from usb.core import USBError
        if dev is None:
            raise Exception('No matching devices found')
        usb.dev = dev
        usb.dev.default_timeout = 15000
        try:
            active = dev.get_active_configuration().bConfigurationValue
        except USBError:
            active = None
        if active != 1:
            dev.set_configuration()
    usb.open_dev = fixed_open_dev

    # (b) 0090 uses unsigned partition table (ROM 6.7 build 164 and similar)
    try:
        init_flash.partition_signature_0090 = b''
    except Exception:
        pass

    # (c) Factory-reset the sensor to clear the corrupted partition table, then
    # cold power-cycle and format.  Retried up to 3x because the table can
    # persist until a cold cycle (issue #287).
    # CRITICAL: usb must be opened before any usb.cmd() call, otherwise usb.dev
    # is None and the reset silently no-ops (this was the real bug).
    from validitysensor.blobs import reset_blob
    from validitysensor.sensor import reboot, RebootException
    from validitysensor.util import assert_status

    formatted = False
    for attempt in range(1, 4):
        log('  === factory reset attempt %d ===' % attempt)
        err = open_usb(usb, retries=10, delay=1.0)
        if err is not None:
            log('  cannot open usb for factory reset: %r' % err)
            usb.dev = None
            continue

        try:
            assert_status(usb.cmd(reset_blob))
            assert_status(usb.cmd(b'\x10' + b'\0' * 0x61))
            reboot()  # sends 050200 and raises RebootException
        except RebootException:
            log('  device rebooting after factory reset (expected)')
        except Exception as exc:
            log('  factory reset error: %r' % exc)
        release(usb)

        # Cold USB power cycle to drop the persisted table.
        port = port_dir()
        if port:
            wr(port + '/disable', 1)
            time.sleep(2)
            wr(port + '/disable', 0)
            time.sleep(2)
        dev = wait_dev(30)
        if dev:
            wr(dev + '/power/control', 'on')
            log('  %s (devnum %s, power=%s)'
                % (dev, rd(dev + '/devnum'), rd(dev + '/power/control')))
        else:
            log('  sensor did not re-enumerate after factory reset')

        # init.open() opens usb fresh and formats the now-empty flash.
        try:
            init.open()
            log('FORMAT_MAYBE: init.open() returned without rebooting')
            formatted = True
            break
        except RebootException:
            log('FORMAT_OK: sensor rebooted after the rebuild')
            formatted = True
            break
        except Exception as exc:
            text = repr(exc)
            lower = text.lower()
            if state['partitioned'] and any(
                    s in lower for s in ('timeout', 'no such device', 'pipe', 'broken')):
                log('FORMAT_MAYBE: device disconnected during the rebuild (%s)' % text)
                formatted = True
                break
            log('  format attempt %d failed: %s' % (attempt, text))

    usb.dev = None
    sys.stdout.flush()
    os._exit(0 if formatted else 1)


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def run_stage(name, timeout=240):
    env = dict(os.environ)
    env['FP_STAGE'] = name
    try:
        res = subprocess.run([sys.executable, os.path.abspath(__file__)],
                             env=env, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, '', 'timeout after %ss' % timeout
    return res.returncode, res.stdout or '', res.stderr or ''


def probe_once():
    rc, out, err = run_stage('probe', timeout=120)
    for line in out.splitlines():
        print(line, flush=True)
    if err.strip():
        print(err.rstrip(), file=sys.stderr, flush=True)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                return json.loads(line)
            except Exception:
                pass
    return {'ok': False, 'error': 'no JSON from probe (rc=%s)' % rc}


def ensure_rule():
    try:
        current = ''
        if os.path.exists(RULE):
            with open(RULE) as fh:
                current = fh.read()
        if current != RULE_TEXT:
            with open(RULE, 'w') as fh:
                fh.write(RULE_TEXT)
            sh('udevadm', 'control', '--reload-rules')
            log('installed %s' % RULE)
    except Exception as exc:
        log('could not install udev rule: %r' % exc)


def kill_strays():
    for pattern in ('/usr/lib/python-validity/dbus-service',
                    '/usr/lib/open-fprintd/open-fprintd'):
        sh('pkill', '-f', pattern)
    end = time.time() + 5
    while time.time() < end:
        if not sh('pgrep', '-af', 'dbus-service|open-fprintd'):
            return
        time.sleep(0.5)


def cleanup():
    log('=== stopping driver stack ===')
    sh('systemctl', 'stop', 'python3-validity.service')
    sh('systemctl', 'mask', 'python3-validity.service')
    sh('systemctl', 'stop', 'open-fprintd.service')
    sh('systemctl', 'stop', 'fprintd.service')
    kill_strays()
    try:
        os.unlink('/run/python-validity/backoff')
        log('cleared restart backoff')
    except FileNotFoundError:
        pass
    except Exception as exc:
        log('could not clear backoff: %r' % exc)


def power_cycle():
    port = port_dir()
    if not port:
        log('  USB port not found; cannot power-cycle')
        return wait_dev(5)

    log('=== power-cycling %s ===' % port)
    wr(port + '/disable', 1)
    time.sleep(2)
    wr(port + '/disable', 0)
    time.sleep(2)
    dev = wait_dev(25)
    if not dev:
        return None
    if rd(dev + '/authorized') == '0':
        wr(dev + '/authorized', 1)
    wr(dev + '/power/control', 'on')
    log('  %s (devnum %s, power=%s)'
        % (dev, rd(dev + '/devnum'), rd(dev + '/power/control')))
    return dev


def journal(lines=120, since=None):
    args = ['journalctl', '-u', 'python3-validity.service']
    if since is not None:
        args += ['--since', '@%d' % int(since)]
    args += ['-n', str(lines), '--no-pager']
    return sh(*args)


def recent_failure(since=None):
    out = journal(since=since)
    codes = re_find_codes(out)
    if codes:
        return '%04x' % codes[-1], out
    if 'Resource busy' in out or 'Errno 16' in out:
        return 'EBUSY', out
    if 'Timeout' in out or 'timed out' in out:
        return 'TIMEOUT', out
    return None, out


def re_find_codes(text):
    return [int(c, 16) for c in re.findall(r'Failed: ([0-9a-fA-F]{4})', text)]


def is_format_status(status):
    """Accept a hex string like '04af' or an int; true when a rebuild is needed."""
    if isinstance(status, int):
        return status in FORMAT_CODES
    if isinstance(status, str):
        try:
            return int(status, 16) in FORMAT_CODES
        except ValueError:
            return False
    return False


def start_and_wait(timeout=150):
    # Only judge failures logged from this attempt onward; older journal lines
    # would otherwise abort a healthy start.
    since = time.time() - 2
    sh('systemctl', 'daemon-reload')
    sh('systemctl', 'unmask', 'python3-validity.service')
    sh('systemctl', 'reset-failed', 'python3-validity.service')
    sh('systemctl', 'start', 'open-fprintd.service')
    time.sleep(1)
    sh('systemctl', 'start', 'python3-validity.service')

    deadline = time.time() + timeout
    restarted = False
    last = ''
    while time.time() < deadline:
        last = sh('fprintd-list', target_user())
        if (last and 'No devices found' not in last
                and 'failed' not in last.lower() and 'error' not in last.lower()):
            return True, last, since

        if sh('systemctl', 'is-failed', 'python3-validity.service').strip() == 'failed':
            text = journal(40, since=since)
            if re_find_codes(text):
                return False, last, since
            if not restarted and ('start-limit-hit' in text or 'too quickly' in text):
                sh('systemctl', 'reset-failed', 'python3-validity.service')
                try:
                    os.unlink('/run/python-validity/backoff')
                except Exception:
                    pass
                sh('systemctl', 'start', 'python3-validity.service')
                restarted = True
        time.sleep(2)
    return False, last, since


def describe(info):
    if 'error' in info:
        log('  probe error: %s' % info['error'])
        return
    log('  3e -> %s (len %s)' % (info.get('status'), info.get('length')))
    for p in info.get('parts', []):
        log('    id=%-3d type=%-3d access=%-5d off=0x%06x size=0x%06x' % tuple(p))
    log('  layout matches 0090: %s' % info.get('layout_ok'))


def main():
    try:
        open(LOG, 'w').close()
    except Exception:
        pass

    if os.geteuid() != 0:
        print('re-running with sudo...', flush=True)
        try:
            os.execvp('sudo', ['sudo', sys.executable, os.path.abspath(__file__)])
        except Exception as exc:
            print('cannot elevate: %r' % exc, flush=True)
            return 1

    if not os.path.isdir('/dev/bus/usb'):
        log('RESULT: /dev/bus/usb is missing; this shell cannot reach the sensor')
        return 1

    sys.path.insert(0, VENDOR)
    result = 1
    try:
        ensure_rule()
        cleanup()

        dev = power_cycle()
        if not dev:
            log('RESULT: sensor did not re-enumerate after the power cycle')
            return 1

        log('=== probing partition table ===')
        info = probe_once()
        describe(info)
        probe_ok = info.get('ok') and info.get('status') == '0000'
        table_ok = probe_ok and info.get('layout_ok')

        if table_ok:
            log('=== starting driver (non-destructive) ===')
            ok, out, since = start_and_wait()
            if ok:
                log('=== fprintd-list ===')
                log(out)
                log('RESULT: sensor recovered without formatting; enrollment is available')
                result = 0
                return result

            status, jout = recent_failure(since)
            log('  driver did not come up (last status: %s)' % status)
            if not is_format_status(status):
                log('=== journal ===')
                log(jout)
                log('RESULT: table is intact but the driver cannot open a session')
                return 1
            log('  certificate/TLS store is unusable (status %s); rebuilding the flash'
                % status)
        elif not probe_ok:
            # No trustworthy view of the flash: this is a USB/session problem,
            # and formatting blind could destroy a good table.
            log('RESULT: could not read the partition table over USB; not formatting')
            return 1
        else:
            log('  table is readable but does not match the 0090 layout; rebuilding')

        log('=== preparing to rebuild the flash ===')
        cleanup()
        dev = power_cycle()
        if not dev:
            log('RESULT: sensor did not re-enumerate before the rebuild')
            return 1

        rc, out, err = run_stage('format', timeout=300)
        for line in out.splitlines():
            print(line, flush=True)
        if err.strip():
            print(err.rstrip(), file=sys.stderr, flush=True)
        if rc != 0:
            log('=== journal ===')
            log(journal(80))
            log('RESULT: formatting failed (stage rc=%s)' % rc)
            return 1

        log('=== verifying rebuilt table ===')
        dev = wait_dev(30) or dev
        if dev:
            wr(dev + '/power/control', 'on')
        time.sleep(2)
        info = probe_once()
        describe(info)
        if not (info.get('ok') and info.get('status') == '0000'
                and info.get('layout_ok')):
            log('RESULT: rebuild ran but the partition table is still invalid')
            return 1

        log('=== starting driver ===')
        ok, out, since = start_and_wait()
        log('=== fprintd-list ===')
        log(out)
        if ok:
            log('RESULT: sensor rebuilt and driver is up; enrollment is available')
            result = 0
            return result

        status, jout = recent_failure(since)
        log('=== journal ===')
        log(jout)
        log('RESULT: rebuild finished but the driver still failed (status: %s)' % status)
        return 1
    finally:
        sh('systemctl', 'daemon-reload')
        sh('systemctl', 'unmask', 'python3-validity.service')
        sh('systemctl', 'reset-failed', 'python3-validity.service')
        if result != 0:
            sh('systemctl', 'stop', 'python3-validity.service')
    return result


if __name__ == '__main__':
    stage = os.environ.get('FP_STAGE')
    if stage == 'probe':
        stage_probe()
    elif stage == 'format':
        stage_format()
    else:
        sys.exit(main())
