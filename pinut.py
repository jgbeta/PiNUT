#!/usr/bin/env python3
"""
PiNUT (minimal): when the power has been out long enough, shut the servers
down over SSH, prove they are off, then turn the UPS output off.

Every POLL_S seconds it reads the UPS through NUT. On mains it does nothing
else. The moment the UPS reports it is on battery, the outage logic starts.

While running, it saves nothing to disk except the reboot count (--calibrate
saves its own CSV). After a restart it reads
the UPS again and starts over; if the UPS is already on battery at that point
(the Pi restarted mid-outage), it shuts the servers down straight away.

    python3 pinut.py                  run (systemd does this)
    python3 pinut.py --shutdown SERVER_NAME  shut one server down now, to test its setup
    python3 pinut.py --calibrate      shut every server down now, and time it

Your settings go in ~/pinut/config.toml (copy config.example.toml). Any
setting below can go there, with the same name; the file wins.
"""

import csv
import logging
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ===========================================================================
# SETTINGS
# ===========================================================================

UPS_NAME = "custom-ups-name"  # the [name] from /etc/nut/ups.conf
UPS = f"{UPS_NAME}@localhost"  # follows UPS_NAME, unless the config sets it
DRY_RUN = True  # only log what it would do. Keep True until a fake outage looks right

SERVERS = [
    # host is pinged, and logged in to as user with key. What the key may run
    # is set on the server, not here (README, step 4). "early": True asks
    # that server EARLY_SHUTDOWN_S into an outage, to save battery.
    {"name": "unraid", "host": "192.168.1.10", "user": "root", "key": "~/.ssh/pinut_unraid"},
    {"name": "ubuntu", "host": "192.168.1.50", "user": "shutdownbot", "key": "~/.ssh/pinut_ubuntu"},
]
SERVER_KEYS = {"name", "host", "user", "key"}  # each server needs all of these
SERVER_OPTIONS = {"early"}  # and may have these

# When to shut the servers down
BLIP_S = 60  # ignore outages shorter than this
MAX_ON_BATTERY_S = 600  # on battery this long: shut down
SHUTDOWN_CHARGE_PCT = 50  # or sooner, once the charge is this low
MIN_RUNTIME_S = 600  # or once the UPS's runtime estimate is below this (after BLIP_S). 0: off
LOW_CHARGE_PCT = 25  # low battery (the UPS's LB, or this charge) shuts down at once,
LOW_CONFIRM_S = 15  # once it has held this long: one odd reading is not enough
EARLY_SHUTDOWN_S = 300  # servers with "early": True are asked this long into an outage

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
DRIVER_SERVICE = f"nut-driver@{UPS_NAME}.service"  # follows UPS_NAME too. "": skip the restart
REBOOT_S = 180  # still no contact: reboot the Pi
MAX_REBOOTS = 2  # at most this many reboots in 24 hours

# --calibrate
CALIBRATE_S = 300  # record the load and ping the servers this long

POLL_S = 5
STATUS_LOG_S = 300  # while shutting down, say what it is waiting for this often
NUT_USER = "upsctl"
HOME = Path.home()
CONFIG_FILE = HOME / "pinut/config.toml"
NUT_PASSWORD_FILE = HOME / "pinut/upscmd.password"
REBOOT_FILE = HOME / "pinut/reboots"  # when it last rebooted the Pi, one time per line
CALIBRATION_DIR = HOME / "pinut"  # --calibrate saves calibration-<date>.csv here

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

SETTINGS = {name for name in dict(globals()) if name.isupper()}  # what the config may set

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
        start_new_session=True,  # Ctrl-C on --shutdown or --calibrate must not abort a request
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
        self.short = Timer()  # runtime estimate below MIN_RUNTIME_S
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
        for timer in (self.outage, self.low, self.short, self.proof):
            timer.update(False)

    def on_battery_poll(self, ups, flags):
        charge, runtime = num(ups, "battery.charge"), num(ups, "battery.runtime")
        if not self.on_battery:
            log.warning("ON BATTERY: charge %s%%, runtime %s, load %s%%", ups.get("battery.charge", "?"),
                        "?" if runtime is None else fmt(runtime), ups.get("ups.load", "?"))
        self.on_battery = True
        on_battery_s = self.outage.update(True)
        self.low.update("LB" in flags or (charge is not None and charge <= LOW_CHARGE_PCT))
        self.short.update(runtime is not None and runtime < MIN_RUNTIME_S)

        if self.shutdown_since is None:
            reason = self.shutdown_reason(on_battery_s, charge, runtime)
            if reason is None:
                if on_battery_s >= EARLY_SHUTDOWN_S:
                    self.shut_down_early()
                return
            self.start_shutdown(reason)
        alive = self.shut_down_servers()
        self.cut_when_proven_off(ups, alive)

    def shutdown_reason(self, on_battery_s, charge, runtime):
        """Why the servers should shut down now, or None to keep waiting."""
        if self.first_reading:
            return "already on battery when PiNUT started (it restarted mid-outage?)"
        if self.low.held() >= LOW_CONFIRM_S:
            return f"low battery for {fmt(self.low.held())} (charge {'?' if charge is None else f'{charge:g}'}%)"
        if on_battery_s < BLIP_S:
            return None
        if on_battery_s >= MAX_ON_BATTERY_S:
            return f"on battery for {fmt(on_battery_s)}"
        if charge is not None and charge <= SHUTDOWN_CHARGE_PCT:
            return f"charge {charge:g}%"
        if self.short.held() >= LOW_CONFIRM_S:
            return f"runtime {fmt(runtime)} left, below MIN_RUNTIME_S {fmt(MIN_RUNTIME_S)}"
        return None

    def shut_down_early(self):
        """Ask the "early" servers once, before the others, so the battery lasts longer."""
        for server in SERVERS:
            if server.get("early") and server["name"] not in self.asked:
                log.warning("%s is early: asking it %s into the outage", server["name"], fmt(EARLY_SHUTDOWN_S))
                self.ask(server)

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
                    if self.asked[name] < self.shutdown_since:
                        log.warning("%s (asked early) does not answer ping", name)
                    else:
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
            log.log(logging.INFO if p.returncode == 0 else logging.WARNING, "%s: %s%s",
                    name, ssh_verdict(p.returncode, err), f": {err[-300:]}" if err else "")
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
# SETTINGS FROM THE CONFIG FILE, AND CHECKING THEM
# ===========================================================================


def kind(value):
    """What sort of value a setting is, in words."""
    for types, words in ((bool, "true or false"), ((int, float), "a number"), (str, "text"),
                         (list, "a list"), (Path, "a path")):
        if isinstance(value, types):
            return words
    return type(value).__name__


def confirm(prompt):
    try:
        return input(prompt).strip().lower() == "yes"
    except EOFError:
        return False


def load_config(interactive):
    """
    Put CONFIG_FILE over the SETTINGS above. Returns (errors, warnings).
    With no file, the settings above are used as they are: in a terminal it
    asks first, under systemd it only warns.
    """
    if not CONFIG_FILE.is_file():
        note = f"no config at {CONFIG_FILE}: using the settings inside pinut.py"
        if interactive and not confirm(f"{note}. Continue? (yes/no) "):
            return [f"no config: cp ~/pinut/config.example.toml {CONFIG_FILE} (README, step 3)"], []
        return [], [note]
    try:
        import tomllib  # part of Python since 3.11
    except ImportError:
        return [f"reading {CONFIG_FILE.name} needs Python 3.11 or newer (this is {sys.version.split()[0]})"], []
    try:
        config = tomllib.loads(CONFIG_FILE.read_text())
    except OSError as e:
        return [f"{CONFIG_FILE}: {e}"], []
    except tomllib.TOMLDecodeError as e:
        hint = '. Text needs "quotes"; true, false (lowercase) and numbers do not' if "Invalid value" in str(e) else ""
        return [f"{CONFIG_FILE}: {e}{hint}"], []

    errors, warnings = [], []
    # Every line below a [[SERVERS]] belongs to that server, so a setting
    # written at the end of the file would silently do nothing.
    servers = config.get("SERVERS")
    for s in servers if isinstance(servers, list) else []:
        for name in sorted(s.keys() & SETTINGS if isinstance(s, dict) else []):
            errors.append(f"{CONFIG_FILE.name}: {name} is inside the [[SERVERS]] for {s.get('name', '?')}: "
                          "move it above the first [[SERVERS]]")
    settings = globals()
    for name, value in config.items():
        if name not in SETTINGS:
            hint = f" (write it {name.upper()})" if name.upper() in SETTINGS else ""
            warnings.append(f"{CONFIG_FILE.name}: {name} is not a PiNUT setting, so it is not used{hint}")
            continue
        default = settings[name]
        if isinstance(default, Path) and isinstance(value, str):
            value = Path(value).expanduser()
        if kind(value) != kind(default):
            errors.append(f"{CONFIG_FILE.name}: {name} should be {kind(default)}, not {value!r}")
            continue
        settings[name] = value
    if "UPS" not in config:
        settings["UPS"] = f"{UPS_NAME}@localhost"
    if "DRIVER_SERVICE" not in config:
        settings["DRIVER_SERVICE"] = f"nut-driver@{UPS_NAME}.service"
    return errors, warnings


def settings_problems():
    """What is wrong with the settings: (errors that stop PiNUT, warnings)."""
    errors, warnings = [], []
    if not SERVERS:
        errors.append("SERVERS is empty: there is nothing to shut down")
    names = [s.get("name") for s in SERVERS if isinstance(s, dict)]
    if len(set(names)) != len(names):
        errors.append("two servers in SERVERS have the same name")
    for s in SERVERS:
        if not isinstance(s, dict):
            errors.append(f"SERVERS: {s!r} should have a name, host, user and key")
            continue
        label = s.get("name", s)
        unknown = s.keys() - SERVER_KEYS - SERVER_OPTIONS
        if unknown:
            warnings.append(f"{label}: {', '.join(sorted(map(str, unknown)))} is not a server setting, so it is not used")
        if not isinstance(s.get("early", False), bool):
            errors.append(f"{label}: early should be true or false")
        missing = SERVER_KEYS - s.keys()
        if missing:
            errors.append(f"{label}: missing {', '.join(sorted(missing))}")
        elif not all(isinstance(s[k], str) for k in SERVER_KEYS):
            errors.append(f"{label}: name, host, user and key should be text")
        elif not Path(s["key"]).expanduser().is_file():
            errors.append(f"{label}: no key at {s['key']} (README, step 4)")
    if not 0 < SAFE_LOAD_PCT < 100:
        errors.append(f"SAFE_LOAD_PCT {SAFE_LOAD_PCT} must be between 0 and 100")
    if not DRY_RUN and not NUT_PASSWORD_FILE.is_file():
        errors.append(f"no NUT password at {NUT_PASSWORD_FILE} (README, step 1)")
    return errors, warnings


# ===========================================================================
# TESTING BY HAND: --shutdown AND --calibrate
# ===========================================================================

# ssh errors that mean the request never reached the server
NOT_SENT = ("Connection timed out", "Connection refused", "No route to host", "Network is unreachable",
            "Could not resolve hostname", "Permission denied", "Host key verification failed")


def ssh_verdict(returncode, err):
    """What an ssh shutdown request's result means, in one line."""
    if returncode == 0:
        return "OK: the request was accepted"
    if any(s in err for s in NOT_SENT):
        return f"FAILED (ssh exit {returncode}): the request never reached the server"
    return f"UNSURE (ssh exit {returncode}): the connection dropped, which a shutdown also does"


def mmss(seconds):
    return f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"


def shutdown_one(name):
    """--shutdown SERVER_NAME: one real request, waited for, so you can see the result."""
    server = next((s for s in SERVERS if isinstance(s, dict) and s.get("name") == name), None)
    if server is None:
        print(f"no server {name!r} in SERVERS: {', '.join(str(s.get('name')) for s in SERVERS)}")
        return 2
    if SERVER_KEYS - server.keys():
        print(f"FAILED: {name} needs a name, host, user and key in SERVERS. Nothing was sent.")
        return 2
    if not Path(server["key"]).expanduser().is_file():
        print(f"FAILED: no key at {server['key']} (README, step 4). Nothing was sent.")
        return 2
    print(f"asking {name} to shut down (for real: DRY_RUN does not apply here)...")
    p = start_ssh(server)
    try:
        err = p.communicate(timeout=SSH_TIMEOUT_S)[1]
    except subprocess.TimeoutExpired:
        p.kill()
        err = p.communicate()[1]
    err = (err or "").strip()
    if err:
        print(err)
    verdict = ssh_verdict(p.returncode, err)
    if p.returncode == 0:
        hint = "Now check that it is really powering off."
    elif verdict.startswith("FAILED"):
        hint = "See the error above."
    else:
        hint = "Check whether it is powering off."
    print(f"{name}: {verdict}. {hint}")
    return 0 if p.returncode == 0 else 1


def calibrate():
    """
    --calibrate: ask every server to shut down at once, for real. Then, for
    CALIBRATE_S, record the UPS load every second and keep pinging them. At
    the end, print how long each took to stop answering, and save the load.
    """
    try:
        status = read_ups()["ups.status"].split()
    except Exception as e:
        print(f"cannot read the UPS ({e}): calibrate needs it")
        return 1
    if "OL" not in status or "OB" in status:
        print(f"the UPS is not on mains ({' '.join(status)}): calibrate only on mains power")
        return 1

    print(f"""
CALIBRATION. This shuts down EVERY server in SERVERS, for real, right now:
    {", ".join(f"{s['name']} ({s['host']})" for s in SERVERS)}
DRY_RUN does not apply here. The UPS output is never turned off.

For {mmss(CALIBRATE_S)} it records the UPS load every second and pings the
servers, then prints how long each took to stop answering, and the load
with them off. Let it run to the end: Ctrl-C stops the measuring, but not
the shutdowns. Either way, you turn the servers back on by hand afterwards.
""")
    if not confirm("Type yes to shut them all down now: "):
        print("Not confirmed: nothing was sent.")
        return 1

    start = time.monotonic()
    ssh = {}  # server name -> ssh process, still running
    for s in SERVERS:
        try:
            ssh[s["name"]] = start_ssh(s)
        except OSError as e:
            print(f"{s['name']}: ssh did not start: {e}")
    verdicts = {}  # server name -> ssh_verdict()

    last_reply = {}  # server name -> seconds into the test of its last ping reply
    stop = threading.Event()

    def pinger():  # in its own thread, so a slow ping never delays the load readings
        while not stop.is_set():
            sent = time.monotonic()
            for name, up in ping_all(SERVERS).items():
                if up:
                    last_reply[name] = sent - start
            stop.wait(max(0.0, 1 - (time.monotonic() - sent)))

    thread = threading.Thread(target=pinger, daemon=True)
    thread.start()
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    path = CALIBRATION_DIR / f"calibration-{datetime.now():%Y%m%d-%H%M%S}.csv"
    loads = []
    try:
        with open(path, "w", newline="") as f:
            rows = csv.writer(f)
            rows.writerow(["time", "load"])
            for second in range(CALIBRATE_S + 1):
                time.sleep(max(0.0, start + second - time.monotonic()))
                try:
                    load = num(read_ups(), "ups.load")
                except Exception:
                    load = None
                loads.append(load)
                rows.writerow([datetime.now().isoformat(sep=" ", timespec="seconds"),
                               "" if load is None else f"{load:g}"])
                f.flush()
                for name, p in list(ssh.items()):
                    if p.poll() is not None:
                        err = (p.communicate()[1] or "").strip()
                        verdicts[name] = ssh_verdict(p.returncode, err) + (f": {err[-300:]}" if err else "")
                        print(f"  {mmss(second)}  {name}: {verdicts[name]}")
                        del ssh[name]
                if second and second % 30 == 0:
                    up = [n for n, t in last_reply.items() if second - t < 3]
                    print(f"  {mmss(second)}  load {'?' if load is None else f'{load:g}'}%, "
                          f"answering ping: {', '.join(up) or 'none'}")
    except KeyboardInterrupt:
        print("\nStopped early: the results below are incomplete.")
    finally:
        stop.set()
        thread.join(timeout=5)

    all_off = print_results(time.monotonic() - start, last_reply, verdicts, ssh, loads)
    print(f"Load every second: {path}")
    for name, p in ssh.items():  # quitting now could cut a request off
        print(f"Waiting for the ssh to {name} to finish (at most {fmt(SSH_TIMEOUT_S)})...")
        try:
            err = p.communicate(timeout=SSH_TIMEOUT_S)[1]
        except subprocess.TimeoutExpired:
            p.kill()
            err = p.communicate()[1]
        err = (err or "").strip()
        print(f"  {name}: {ssh_verdict(p.returncode, err)}" + (f": {err[-300:]}" if err else ""))
    print("Turn the servers back on by hand.")
    return 0 if all_off else 1


def print_results(elapsed, last_reply, verdicts, running, loads):
    """The end of --calibrate. Returns True if every server stopped answering ping."""
    all_off = True
    width = max(len(s["name"]) for s in SERVERS)
    print(f"\nRESULTS after {mmss(elapsed)}:")
    for s in SERVERS:
        name, last = s["name"], last_reply.get(s["name"])
        if last is None:
            all_off = False
            result = "never answered ping, so PiNUT cannot tell when it is off: check its host and firewall"
        elif elapsed - last < 10:
            all_off = False
            result = f"STILL ANSWERING ping after {mmss(elapsed)}"
        else:
            result = f"stopped answering ping after {mmss(last)}"
        print(f"  {name:<{width}}  {result}")
        if name in running:
            print(f"  {'':<{width}}  ssh still running: see below")
        elif not verdicts.get(name, "OK").startswith("OK"):
            print(f"  {'':<{width}}  ssh {verdicts[name]}")

    recent = [load for load in loads[-10:] if load is not None]
    if not recent:
        print("\nThe UPS reported no load.")
    else:
        print(f"\nAverage load, last 10 s: {sum(recent) / len(recent):.1f}%")
        if all_off:
            suggested = math.ceil(max(recent)) + 2
            print(f"Suggested SAFE_LOAD_PCT: {suggested} (the highest of those, plus 2). Yours is {SAFE_LOAD_PCT}"
                  + (", below it: PiNUT could never prove them off" if SAFE_LOAD_PCT < max(recent) else ""))
        else:
            print("Not every server stopped answering, so this is not the load with them all off.")
    return all_off


# ===========================================================================
# STARTING UP
# ===========================================================================


def main():
    # No timestamps: journald adds them.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:]
    if args not in ([], ["--calibrate"]) and not (len(args) == 2 and args[0] == "--shutdown"):
        print(__doc__.strip())
        return 2

    errors, warnings = load_config(interactive=sys.stdin.isatty())
    if not errors and args[:1] != ["--shutdown"]:  # testing one server must not need the others' keys
        more_errors, more_warnings = settings_problems()
        errors, warnings = errors + more_errors, warnings + more_warnings
    for w in warnings:
        log.warning("SETTINGS: %s", w)
    for e in errors:
        log.error("SETTINGS: %s", e)
    if errors:
        log.error("PiNUT NOT STARTED: fix the settings above%s",
                  "" if args else ", then: sudo systemctl restart pinut")
        return 2  # pinut.service does not restart on exit 2: it would not help
    if args == ["--calibrate"]:
        return calibrate()
    if args:
        return shutdown_one(args[1])

    log.warning("PiNUT started: ups=%s, servers=%s, settings from %s%s", UPS,
                ", ".join(s["name"] for s in SERVERS), CONFIG_FILE if CONFIG_FILE.is_file() else "pinut.py",
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
