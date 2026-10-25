#!/usr/bin/env python3
"""
Fake outages, played through pinut.py's logic on a fake clock. No UPS, no
servers, no root, nothing is sent anywhere.

    python3 test_pinut.py        (or: python3 -m unittest -v test_pinut)

Each server "powers off" a few seconds after it is asked. The UPS load is 3%
plus 20% per server still on, so it drops below SAFE_LOAD_PCT (5%) only
when all of them are off.
"""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pinut

# pinut's log lines would bury the results; the tests check what it does, not what it says.
logging.getLogger("pinut").disabled = True


class FakeSsh:
    """Stands in for the ssh process: finishes `runs_s` seconds after it starts."""

    def __init__(self, test, runs_s):
        self.test, self.done_at, self.returncode = test, test.t + runs_s, None

    def poll(self):
        if self.test.t >= self.done_at:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9

    def communicate(self):
        return "", ""


class Outage(unittest.TestCase):
    def setUp(self):
        self.t = 0.0  # the fake clock, in seconds
        self.status, self.charge = "OL", 100
        self.extra = {}  # more UPS readings, e.g. ups.timer.shutdown
        self.no_contact = False
        self.ping_broken = False
        self.up = {"nas": True, "pc": True}
        self.off_after = {"nas": 40, "pc": 20}  # seconds from request to off; None = never goes off
        self.going_off = {}  # name -> fake time it stops answering
        self.ssh_runs_s = 1
        self.asked, self.commands, self.root = [], [], []

        tmp = Path(tempfile.mkdtemp())
        self.reboot_file = tmp / "reboots"
        for name, value in {
            "DRY_RUN": False,
            "SERVERS": [
                {"name": "nas", "host": "10.0.0.1", "user": "root", "key": "~/k1"},
                {"name": "pc", "host": "10.0.0.2", "user": "bot", "key": "~/k2"},
            ],
            "REBOOT_FILE": self.reboot_file,
            "ping_all": self.fake_ping,
            "start_ssh": self.fake_ssh,
            "ups_command": self.commands.append,
            "run_as_root": lambda args: self.root.append(" ".join(args)),
        }.items():
            patcher = mock.patch.object(pinut, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in {"monotonic": lambda: self.t, "time": lambda: 1_700_000_000 + self.t}.items():
            patcher = mock.patch.object(pinut.time, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.guard = pinut.Guard()

    # --- the fake world ----------------------------------------------------

    def fake_ping(self, servers):
        for name, at in list(self.going_off.items()):
            if self.t >= at:
                self.up[name] = False
                del self.going_off[name]
        return {s["name"]: None if self.ping_broken else self.up[s["name"]] for s in servers}

    def fake_ssh(self, server):
        name = server["name"]
        self.asked.append((self.t, name))
        if self.off_after[name] is not None and self.up[name]:
            self.going_off.setdefault(name, self.t + self.off_after[name])
        return FakeSsh(self, self.ssh_runs_s)

    def reading(self):
        if self.no_contact:
            return None
        load = 3 + 20 * sum(self.up.values())
        return {"ups.status": self.status, "battery.charge": str(self.charge), "ups.load": str(load), **self.extra}

    def play(self, seconds):
        """Poll every 5 s for `seconds`, the way main() does."""
        end = self.t + seconds
        while self.t < end:
            self.guard.poll(self.reading(), "fake error" if self.no_contact else None)
            self.t += pinut.POLL_S

    def first_cut(self):
        return self.cut_times[0] if self.cut_times else None

    def play_logging_cuts(self, seconds):
        self.cut_times = []
        original = pinut.ups_command

        def record(cmd):
            self.cut_times.append(self.t)
            original(cmd)

        with mock.patch.object(pinut, "ups_command", record):
            self.play(seconds)

    def outage(self):
        self.play(30)  # on mains first, like a real start
        self.status = "OB DISCHRG"

    # --- when to shut down -------------------------------------------------

    def test_blip_is_ignored(self):
        self.outage()
        self.play(55)
        self.status = "OL"
        self.play(600)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.commands, [])

    def test_long_outage_asks_everyone_at_once(self):
        self.up["pc"] = False  # already off: asked anyway, ping is not proof it was ever on
        self.outage()
        start = self.t
        self.play(pinut.MAX_ON_BATTERY_S - 5)
        self.assertEqual(self.asked, [])
        self.play(10)
        self.assertEqual(sorted(n for _, n in self.asked), ["nas", "pc"])
        self.assertEqual({t for t, _ in self.asked}, {start + pinut.MAX_ON_BATTERY_S})

    def test_half_charge_after_the_blip_wait(self):
        self.charge = 50
        self.outage()
        self.play(pinut.BLIP_S - 5)
        self.assertEqual(self.asked, [])
        self.play(10)
        self.assertEqual(len(self.asked), 2)

    def test_low_battery_skips_the_blip_wait_once_confirmed(self):
        self.outage()
        start = self.t
        self.status = "OB DISCHRG LB"
        self.play(5)  # one reading is not enough
        self.status = "OB DISCHRG"
        self.play(20)
        self.assertEqual(self.asked, [])
        self.status = "OB DISCHRG LB"
        self.play(pinut.LOW_CONFIRM_S + 5)
        self.assertEqual(len(self.asked), 2)
        self.assertLess(self.t - start, pinut.BLIP_S)

    def test_started_during_an_outage_shuts_down_at_once(self):
        self.status = "OB DISCHRG"
        self.play(5)
        self.assertEqual(len(self.asked), 2)

    def test_unclear_status_does_nothing(self):
        self.status = "OL OB"
        self.play(1200)
        self.assertEqual(self.asked, [])

    # --- proving off, cutting ----------------------------------------------

    def test_full_outage_cuts_once_after_the_proof(self):
        self.outage()
        self.play_logging_cuts(pinut.MAX_ON_BATTERY_S + 300)
        nas_off = 30 + pinut.MAX_ON_BATTERY_S + self.off_after["nas"]
        self.assertEqual(self.commands, ["load.off.delay"] * len(self.cut_times))
        # The first poll after both are off starts the proof; it needs PROOF_S unbroken.
        self.assertGreaterEqual(self.first_cut(), nas_off + pinut.PROOF_S)
        self.assertLessEqual(self.first_cut(), nas_off + pinut.PROOF_S + 10)

    def test_no_resend_while_the_ups_counts_down(self):
        self.outage()
        self.extra = {"ups.timer.shutdown": "20"}
        self.play(pinut.MAX_ON_BATTERY_S + 1200)
        self.assertEqual(self.commands, [])  # countdown already running (it never ends in this fake)
        self.extra = {"ups.timer.shutdown": "-1", "ups.delay.shutdown": "20"}
        self.play_logging_cuts(400)
        self.assertEqual(len(self.commands), 3)  # every CUT_RESEND_S + 20 s while still on battery
        self.assertEqual(self.cut_times[1] - self.cut_times[0], pinut.CUT_RESEND_S + 20)

    def test_a_server_that_stays_on_is_asked_again_and_never_cut(self):
        self.off_after["nas"] = None
        self.outage()
        self.play(pinut.MAX_ON_BATTERY_S + 3 * pinut.RESEND_S + 10)
        self.assertEqual([n for _, n in self.asked].count("nas"), 4)
        self.assertEqual([n for _, n in self.asked].count("pc"), 1)
        self.assertEqual(self.commands, [])

    def test_no_second_request_while_the_first_is_running(self):
        self.off_after["nas"] = None
        self.ssh_runs_s = 10_000  # like Unraid's powerdown, holding the session
        self.outage()
        self.play(pinut.MAX_ON_BATTERY_S + pinut.RESEND_S + 10)
        self.assertEqual([n for _, n in self.asked].count("nas"), 1)

    def test_ping_failure_never_counts_as_off(self):
        self.outage()
        self.play(pinut.MAX_ON_BATTERY_S + 100)
        self.ping_broken = True
        self.play(1200)
        self.assertEqual(self.commands, [])

    def test_missing_or_high_load_never_counts_as_off(self):
        self.outage()
        self.play(pinut.MAX_ON_BATTERY_S + 50)  # both off now
        with mock.patch.object(pinut, "SAFE_LOAD_PCT", 2):  # 3% left: above the limit
            self.play(600)
        self.assertEqual(self.commands, [])
        original = self.reading
        self.reading = lambda: {k: v for k, v in original().items() if k != "ups.load"}
        self.play(600)
        self.assertEqual(self.commands, [])

    def test_a_server_back_on_restarts_the_proof(self):
        self.outage()
        self.play(pinut.MAX_ON_BATTERY_S + 80)  # both off, proof running
        self.up["pc"] = True  # came back (rebooted instead of powering off)
        self.play(5)
        back = self.t
        self.up["pc"] = False
        self.play_logging_cuts(300)
        self.assertGreaterEqual(self.first_cut(), back + pinut.PROOF_S)

    def test_mains_back_stops_everything(self):
        self.outage()
        self.play(pinut.MAX_ON_BATTERY_S + 60)  # asked, both off, proof running
        self.status = "OL CHRG"
        self.play(600)
        self.assertEqual(self.commands, [])
        # The next outage starts from scratch: blip wait, then a fresh request.
        asked = len(self.asked)
        self.status = "OB DISCHRG"
        self.play(pinut.BLIP_S - 5)
        self.assertEqual(len(self.asked), asked)

    def test_dry_run_sends_nothing(self):
        with mock.patch.object(pinut, "DRY_RUN", True):
            self.up = {"nas": False, "pc": False}  # so the proof holds and the cut is reached
            self.outage()
            self.play(pinut.MAX_ON_BATTERY_S + 600)
            self.no_contact = True
            self.play(600)
        self.assertEqual((self.asked, self.commands, self.root), ([], [], []))
        self.assertFalse(self.reboot_file.exists())

    # --- lost contact ------------------------------------------------------

    def test_lost_contact_on_battery_shuts_down_but_never_cuts(self):
        self.outage()
        self.play(10)
        self.no_contact = True
        self.play(pinut.LOST_ON_BATTERY_S - 5)
        self.assertEqual(self.asked, [])
        self.play(10)
        self.assertEqual(len(self.asked), 2)
        self.play(170)  # the reboot came, but the fake Pi keeps running
        self.assertEqual(self.commands, [])

    def test_lost_contact_restarts_the_driver_then_reboots(self):
        self.play(30)
        self.no_contact = True
        self.play(pinut.DRIVER_RESTART_S + 5)
        self.assertEqual(self.root, ["/usr/bin/systemctl restart " + pinut.DRIVER_SERVICE])
        self.play(pinut.REBOOT_S - pinut.DRIVER_RESTART_S)
        self.assertEqual(self.root[1:], ["/usr/bin/systemctl reboot"])
        self.assertEqual(len(self.reboot_file.read_text().split()), 1)
        self.assertEqual(self.asked, [])  # on mains: nothing to shut down

    def test_reboots_are_limited(self):
        self.reboot_file.write_text(f"{1_700_000_000 - 3600}\n{1_700_000_000 - 60}\n")
        self.no_contact = True
        self.play(3600)
        self.assertNotIn("/usr/bin/systemctl reboot", self.root)

    def test_old_reboots_do_not_count(self):
        self.reboot_file.write_text(f"{1_700_000_000 - 90_000}\n{1_700_000_000 - 89_000}\n")
        self.no_contact = True
        self.play(pinut.REBOOT_S + 5)
        self.assertIn("/usr/bin/systemctl reboot", self.root)

    def test_no_reboot_while_a_request_is_running(self):
        self.ssh_runs_s = 10_000
        self.outage()
        self.play(pinut.MAX_ON_BATTERY_S + 10)
        self.no_contact = True
        self.play(600)
        self.assertNotIn("/usr/bin/systemctl reboot", self.root)

    def test_lost_contact_breaks_the_proof(self):
        self.outage()
        self.play(pinut.MAX_ON_BATTERY_S + 80)  # both off, proof running
        self.no_contact = True
        self.play(5)
        self.no_contact = False
        back = self.t
        self.play_logging_cuts(300)
        self.assertGreaterEqual(self.first_cut(), back + pinut.PROOF_S)

    # --- settings ----------------------------------------------------------

    def test_settings_errors(self):
        with mock.patch.object(pinut, "SERVERS", []):
            self.assertTrue(any("empty" in e for e in pinut.settings_errors()))
        errors = pinut.settings_errors()  # the test servers' keys do not exist
        self.assertTrue(any("nas: no key" in e for e in errors))
        dup = [{"name": "a", "host": "h", "user": "u", "key": "k"}] * 2
        with mock.patch.object(pinut, "SERVERS", dup):
            self.assertTrue(any("same name" in e for e in pinut.settings_errors()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
