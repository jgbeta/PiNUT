#!/usr/bin/env python3
"""
PiNUT (minimal): when the power has been out long enough, shut the servers
down over SSH, prove they are off, then turn the UPS output off.

Every POLL_S seconds it reads the UPS through NUT. On mains it does nothing
else. The moment the UPS reports it is on battery, the outage logic starts.

Nothing is saved to disk except the reboot count. After a restart it reads
the UPS again and starts over; if the UPS is already on battery at that point
(the Pi restarted mid-outage), it shuts the servers down straight away.

    python3 pinut.py                  run (systemd does this)
    python3 pinut.py --shutdown SERVER_NAME  shut one server down now, to test its setup
"""

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ===========================================================================
# SETTINGS
# ===========================================================================

UPS = "custom-ups-name@localhost"  # the [name] from /etc/nut/ups.conf
DRY_RUN = True  # only log what it would do. Keep True until a fake outage looks right

SERVERS = [
    # host is pinged, and logged in to as user with key. What the key may run
    # is set on the server, not here (README, step 4).
    {"name": "unraid", "host": "192.168.1.10", "user": "root", "key": "~/.ssh/pinut_unraid"},
    {"name": "ubuntu", "host": "192.168.1.50", "user": "shutdownbot", "key": "~/.ssh/pinut_ubuntu"},
]

# When to shut the servers down
BLIP_S = 60  # ignore outages shorter than this
MAX_ON_BATTERY_S = 600  # on battery this long: shut down
SHUTDOWN_CHARGE_PCT = 50  # or sooner, once the charge is this low
LOW_CHARGE_PCT = 25  # low battery (the UPS's LB, or this charge) shuts down at once,
LOW_CONFIRM_S = 15  # once it has held this long: one odd reading is not enough

# Shutting them down
RESEND_S = 300  # a server still answering ping this long after the last request is asked again
SSH_TIMEOUT_S = 600  # give up on an ssh still running after this (Unraid's waits for the array to stop)

# Proving they are off, then cutting the output
SAFE_LOAD_PCT = 5  # UPS load (%) with every server off. Measure yours (README, step 6)
PROOF_S = 90  # no server answers ping and the load is at most SAFE_LOAD_PCT, unbroken, this long
CUT_COMMAND = "load.off.delay"  # turns the output off after the UPS's own delay
CUT_RESEND_S = 120  # still on battery, no countdown, this long after the delay: send it again

# Lost contact with the UPS
LOST_ON_BATTERY_S = 60  # lost while on battery: shut the servers down after this (never cut)
DRIVER_RESTART_S = 60  # restart the NUT driver once after this
DRIVER_SERVICE = "nut-driver@custom-ups-name.service"  # None: skip the driver restart
REBOOT_S = 180  # still no contact: reboot the Pi
MAX_REBOOTS = 2  # at most this many reboots in 24 hours

POLL_S = 5
STATUS_LOG_S = 300  # while shutting down, say what it is waiting for this often
NUT_USER = "upsctl"
HOME = Path.home()
NUT_PASSWORD_FILE = HOME / "pinut/upscmd.password"
REBOOT_FILE = HOME / "pinut/reboots"  # when it last rebooted the Pi, one time per line

SSH_OPTS = [
    "-F", "/dev/null",  # ignore ~/.ssh/config, everything is set here
    "-o", "BatchMode=yes",  # never stop at a prompt
    "-o", "StrictHostKeyChecking=yes",  # unknown or changed host key: refuse
    "-o", "IdentitiesOnly=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=5",  # notice within ~10 s when the server goes away
    "-o", "ServerAliveCountMax=2",
    "-o", "RequestTTY=no",  # Windows only enforces ForceCommand without a TTY
    "-o", "ClearAllForwardings=yes",
]

log = logging.getLogger("pinut")

# ===========================================================================
# TALKING TO THINGS
# Everything that touches the UPS, the network or the Pi is in these
# functions, so test_pinut.py can swap them for fakes.
# ===========================================================================


def read_ups():
    """Everything `upsc` reports, as a dict of strings. Raises if it fails."""
    p = subprocess.run(["upsc", UPS], capture_output=True, text=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or "upsc failed")
    reading = {}
    for line in p.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            reading[key.strip()] = value.strip()
    if "ups.status" not in reading:
        raise RuntimeError("upsc gave no ups.status")
    return reading


def ping_all(servers):
    """
    Ping every server at once. For each name: True (answered), False (no
    answer) or None (ping itself failed). None proves nothing, so it never
    counts as off.
    """
    procs = {}
    for s in servers:
        try:
            procs[s["name"]] = subprocess.Popen(
                ["ping", "-n", "-c", "1", "-W", "1", s["host"]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            procs[s["name"]] = None
    result = {}
    for name, p in procs.items():
        code = None
        if p is not None:
            try:
                code = p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        result[name] = {0: True, 1: False}.get(code)
    return result


def start_ssh(server):
    """
    Log in with the server's key, in the background. The server runs its
    fixed shutdown command whatever we send, so the request is a constant.
    """
    return subprocess.Popen(
        ["ssh", *SSH_OPTS, "-i", os.path.expanduser(server["key"]), "-T",
         f"{server['user']}@{server['host']}", "request-shutdown"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )


def ups_command(cmd):
    """Send an instant command to the UPS."""
    try:
        password = NUT_PASSWORD_FILE.read_text().strip()
        p = subprocess.run(
            ["upscmd", "-u", NUT_USER, "-p", password, "-w", "-t", "15", UPS, cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        log.error("UPS command %s did not run: %s", cmd, e)
        return
    if p.returncode != 0:
        log.error("UPS command %s failed: %s", cmd, (p.stderr or p.stdout).strip())
    else:
        log.warning("UPS command %s: %s", cmd, p.stdout.strip())


def run_as_root(args):
    """Run one of the two commands the sudoers rule allows (README, step 5)."""
    try:
        p = subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=60)
    except Exception as e:
        log.error("%s did not run: %s", " ".join(args), e)
        return
    if p.returncode != 0:
        log.error("%s failed: %s", " ".join(args), (p.stderr or p.stdout).strip())


def read_reboots():
    try:
        return [float(t) for t in REBOOT_FILE.read_text().split()]
    except (OSError, ValueError):
        return []


def write_reboots(times):
    REBOOT_FILE.write_text("".join(f"{t:.0f}\n" for t in times))


# ===========================================================================
# THE LOGIC
# ===========================================================================


def num(ups, key):
    """A reading as a number, or None if the UPS does not report it."""
    try:
        return float(ups[key])
    except (KeyError, ValueError):
        return None


def fmt(seconds):
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.0f}min"


class Timer:
    """How long a condition has held without a break, on the monotonic clock."""

    def __init__(self):
        self.since = None

    def update(self, condition):
        """Feed it the condition every poll. Returns how long it has held."""
        if not condition:
            self.since = None
        elif self.since is None:
            self.since = time.monotonic()
        return self.held()

    def held(self):
        return 0 if self.since is None else time.monotonic() - self.since


class Guard:
    def __init__(self):
        self.outage = Timer()  # on battery
        self.low = Timer()  # low battery
        self.proof = Timer()  # every server proven off
        self.no_contact = Timer()  # upsc failing
        self.first_reading = True
        self.on_battery = False  # what the last good reading said
        self.shutdown_since = None  # when the shutdown began
        self.asked = {}  # server name -> when it was last asked to shut down
        self.down_since = {}  # server name -> when it stopped answering ping
        self.last_cut = None  # when CUT_COMMAND was last sent
        self.ssh = {}  # server name -> (ssh process, start time), still running
        self.driver_restarted = False
        self.reboot_after = REBOOT_S
        self.said = {}  # for every()

    def every(self, key, seconds):
        """True at most once every `seconds` per key: keeps repeated messages rare."""
        now = time.monotonic()
        if key in self.said and now - self.said[key] < seconds:
            return False
        self.said[key] = now
        return True

    # One poll: one UPS reading (or None and the error, if upsc failed).
    def poll(self, ups, error=None):
        self.collect_ssh()
        if ups is None:
            self.lost_contact(error)
            return

        if self.no_contact.since is not None:
            log.warning("UPS contact back after %s", fmt(self.no_contact.held()))
        self.no_contact.update(False)
        self.driver_restarted = False
        self.reboot_after = REBOOT_S

        flags = ups["ups.status"].split()
        if "OB" in flags and "OL" not in flags:
            self.on_battery_poll(ups, flags)
        elif "OL" in flags and "OB" not in flags:
            self.on_mains_poll()
        else:
            self.proof.update(False)
            if self.every("unclear", 600):
                log.error("unclear UPS status %r: doing nothing", ups["ups.status"])
        self.first_reading = False

    def on_mains_poll(self):
        if self.on_battery:
            log.warning("MAINS BACK after %s on battery", fmt(self.outage.held()))
            if self.shutdown_since is not None:
                log.warning("asked to shut down, so they stay off: %s", ", ".join(self.asked))
        self.on_battery = False
        self.shutdown_since = None
        self.asked.clear()
        self.down_since.clear()
        self.last_cut = None
        for timer in (self.outage, self.low, self.proof):
            timer.update(False)

    def on_battery_poll(self, ups, flags):
        charge = num(ups, "battery.charge")
        if not self.on_battery:
            log.warning("ON BATTERY: charge %s%%, load %s%%",
                        ups.get("battery.charge", "?"), ups.get("ups.load", "?"))
        self.on_battery = True
        on_battery_s = self.outage.update(True)
        low_s = self.low.update("LB" in flags or (charge is not None and charge <= LOW_CHARGE_PCT))

        if self.shutdown_since is None:
            reason = self.shutdown_reason(on_battery_s, low_s, charge)
            if reason is None:
                return
            self.start_shutdown(reason)
        alive = self.shut_down_servers()
        self.cut_when_proven_off(ups, alive)

    def shutdown_reason(self, on_battery_s, low_s, charge):
        """Why the servers should shut down now, or None to keep waiting."""
        if self.first_reading:
            return "already on battery when PiNUT started (it restarted mid-outage?)"
        if low_s >= LOW_CONFIRM_S:
            return f"low battery for {fmt(low_s)} (charge {'?' if charge is None else f'{charge:g}'}%)"
        if on_battery_s < BLIP_S:
            return None
        if on_battery_s >= MAX_ON_BATTERY_S:
            return f"on battery for {fmt(on_battery_s)}"
        if charge is not None and charge <= SHUTDOWN_CHARGE_PCT:
            return f"charge {charge:g}%"
        return None

    def start_shutdown(self, reason):
        log.warning("SHUTTING THE SERVERS DOWN: %s", reason)
        self.shutdown_since = time.monotonic()

    def shut_down_servers(self):
        """Ask every server once, then again while it answers ping. Returns the ping results."""
        now = time.monotonic()
        alive = ping_all(SERVERS)
        for server in SERVERS:
            name = server["name"]
            if name not in self.asked:  # the first time, all of them, whatever ping says
                self.ask(server)
            elif alive[name] is not False and now - self.asked[name] >= RESEND_S:
                log.warning("%s %s %s after the last request: asking again", name,
                            "still answering ping" if alive[name] else "not proven off (ping failed)",
                            fmt(now - self.asked[name]))
                self.ask(server)

            if alive[name] is False:
                if name not in self.down_since:
                    self.down_since[name] = now
                    log.warning("%s stopped answering ping %s after the shutdown began",
                                name, fmt(now - self.shutdown_since))
            elif self.down_since.pop(name, None) is not None and alive[name]:
                log.warning("%s answers ping again", name)
        return alive

    def ask(self, server):
        name = server["name"]
        self.asked[name] = time.monotonic()
        if name in self.ssh:
            log.warning("%s: the last request is still running, not sending another", name)
        elif DRY_RUN:
            log.warning("DRY RUN: would ask %s to shut down", name)
        else:
            log.warning("asking %s to shut down", name)
            try:
                self.ssh[name] = (start_ssh(server), time.monotonic())
            except OSError as e:
                log.error("%s: ssh did not start: %s", name, e)

    def collect_ssh(self):
        """Log the ssh requests that finished, stop ones running too long."""
        for name, (p, started) in list(self.ssh.items()):
            if p.poll() is None:
                if time.monotonic() - started < SSH_TIMEOUT_S:
                    continue
                p.kill()
                log.error("%s: ssh still running after %s, stopped it", name, fmt(SSH_TIMEOUT_S))
            err = (p.communicate()[1] or "").strip()
            if p.returncode == 0:
                log.info("%s: shutdown request accepted %s", name, err)
            else:
                # Also what a shutdown that cuts its own connection looks like.
                log.warning("%s: ssh ended with exit %s%s", name, p.returncode, f": {err[-300:]}" if err else "")
            del self.ssh[name]

    def cut_when_proven_off(self, ups, alive):
        """Cut the output only after the proof has held for PROOF_S, re-checked every poll."""
        load = num(ups, "ups.load")
        all_off = bool(alive) and all(up is False for up in alive.values())
        off = all_off and load is not None and load <= SAFE_LOAD_PCT
        was_off = self.proof.since is not None
        held = self.proof.update(off)

        if not off:
            if was_off:
                log.warning("not proven off any more (%s): proof starts over", self.why_not_off(alive, load))
            elif self.every("waiting", STATUS_LOG_S):
                log.warning("waiting %s after the shutdown began: %s",
                            fmt(time.monotonic() - self.shutdown_since), self.why_not_off(alive, load))
            return
        if not was_off:
            log.warning("every server looks off, load %s%%: cutting the output if that holds for %s",
                        ups.get("ups.load"), fmt(PROOF_S))
        if held < PROOF_S:
            return

        countdown = num(ups, "ups.timer.shutdown")
        if countdown is not None and countdown > 0:
            return  # the UPS is already counting down to the cut
        wait = CUT_RESEND_S + (num(ups, "ups.delay.shutdown") or 0)
        if self.last_cut is not None and time.monotonic() - self.last_cut < wait:
            return
        again = self.last_cut is not None
        self.last_cut = time.monotonic()
        if DRY_RUN:
            log.warning("DRY RUN: would send %s", CUT_COMMAND)
            return
        log.critical("servers proven off for %s: sending %s%s", fmt(held), CUT_COMMAND,
                     " again, the UPS is still on battery" if again else "")
        ups_command(CUT_COMMAND)

    def why_not_off(self, alive, load):
        up = [n for n, a in alive.items() if a]
        unknown = [n for n, a in alive.items() if a is None]
        parts = []
        if up:
            parts.append("still answering ping: " + ", ".join(up))
        if unknown:
            parts.append("ping failed for: " + ", ".join(unknown))
        if load is None:
            parts.append("the UPS reports no load")
        elif load > SAFE_LOAD_PCT:
            parts.append(f"load {load:g}% is above SAFE_LOAD_PCT {SAFE_LOAD_PCT}%")
        if DRY_RUN:
            parts.append("dry run, so nothing was really shut down")
        return "; ".join(parts)

    def lost_contact(self, error):
        if self.no_contact.since is None:
            log.error("lost contact with the UPS: %s", error)
        lost_s = self.no_contact.update(True)
        self.proof.update(False)  # the cut needs fresh readings

        if self.on_battery and lost_s >= LOST_ON_BATTERY_S:
            if self.shutdown_since is None:
                self.start_shutdown(f"no UPS contact for {fmt(lost_s)} while on battery")
            self.shut_down_servers()

        if DRIVER_SERVICE and not self.driver_restarted and lost_s >= DRIVER_RESTART_S:
            self.driver_restarted = True
            log.warning("no UPS contact for %s: restarting %s", fmt(lost_s), DRIVER_SERVICE)
            if not DRY_RUN:
                run_as_root(["/usr/bin/systemctl", "restart", DRIVER_SERVICE])

        if lost_s >= self.reboot_after:
            self.reboot_after += REBOOT_S  # if this reboot fails, the next try is REBOOT_S later
            self.reboot(lost_s)

    def reboot(self, lost_s):
        if self.ssh:
            log.warning("not rebooting while a shutdown request is still running: %s", ", ".join(self.ssh))
            return
        now = time.time()
        # A time "in the future" means the clock went back: count it as recent.
        recent = [t for t in read_reboots() if now - t < 86400]
        if len(recent) >= MAX_REBOOTS:
            if self.every("reboot_limit", 3600):
                log.critical("no UPS contact, but already rebooted %d times in 24 h: not rebooting", len(recent))
            return
        if DRY_RUN:
            log.warning("DRY RUN: would reboot the Pi (no UPS contact for %s)", fmt(lost_s))
            return
        write_reboots(recent + [now])
        log.critical("REBOOTING THE PI: no UPS contact for %s (%d of %d in 24 h)",
                     fmt(lost_s), len(recent) + 1, MAX_REBOOTS)
        run_as_root(["/usr/bin/systemctl", "reboot"])


# ===========================================================================
# STARTING UP
# ===========================================================================


def settings_errors():
    errors = []
    if not SERVERS:
        errors.append("SERVERS is empty: there is nothing to shut down")
    names = [s.get("name") for s in SERVERS]
    if len(set(names)) != len(names):
        errors.append("two servers in SERVERS have the same name")
    for s in SERVERS:
        missing = {"name", "host", "user", "key"} - s.keys()
        if missing:
            errors.append(f"{s.get('name', s)}: missing {', '.join(sorted(missing))}")
        elif not Path(s["key"]).expanduser().is_file():
            errors.append(f"{s['name']}: no key at {s['key']} (README, step 4)")
    if not 0 < SAFE_LOAD_PCT < 100:
        errors.append(f"SAFE_LOAD_PCT {SAFE_LOAD_PCT} must be between 0 and 100")
    if not DRY_RUN and not NUT_PASSWORD_FILE.is_file():
        errors.append(f"no NUT password at {NUT_PASSWORD_FILE} (README, step 2)")
    return errors


def shutdown_one(name):
    """--shutdown SERVER_NAME: one real request, waited for, so you can see the result."""
    server = next((s for s in SERVERS if s["name"] == name), None)
    if server is None:
        print(f"no server {name!r} in SERVERS: {', '.join(s['name'] for s in SERVERS)}")
        return 2
    print(f"asking {name} to shut down (for real: DRY_RUN does not apply here)...")
    p = start_ssh(server)
    try:
        err = p.communicate(timeout=SSH_TIMEOUT_S)[1]
    except subprocess.TimeoutExpired:
        p.kill()
        err = p.communicate()[1]
    print((err or "").strip())
    print(f"ssh exit {p.returncode}. Now check that {name} is really powering off.")
    return 0 if p.returncode == 0 else 1


def main():
    # No timestamps: journald adds them.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) == 3 and sys.argv[1] == "--shutdown":
        return shutdown_one(sys.argv[2])
    if len(sys.argv) != 1:
        print(__doc__.strip())
        return 2

    errors = settings_errors()
    if errors:
        for e in errors:
            log.error("SETTINGS: %s", e)
        log.error("PiNUT NOT STARTED: fix the settings above, then: sudo systemctl restart pinut")
        return 2  # pinut.service does not restart on exit 2: it would not help

    log.warning("PiNUT started: ups=%s, servers=%s%s", UPS, ", ".join(s["name"] for s in SERVERS),
                ", DRY RUN (nothing is really shut down or cut)" if DRY_RUN else "")
    guard = Guard()
    while True:
        try:
            ups, error = read_ups(), None
        except Exception as e:
            ups, error = None, e
        guard.poll(ups, error)
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
