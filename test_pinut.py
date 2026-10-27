#!/usr/bin/env python3
"""
Fake outages, played through pinut.py's logic on a fake clock, plus checks
of the config file, --shutdown and --calibrate. No UPS, no servers, no
root, nothing is sent anywhere.

    python3 test_pinut.py        (or: python3 -m unittest -v test_pinut)

Each server "powers off" a few seconds after it is asked. The UPS load is 3%
plus 20% per server still on, so it drops below SAFE_LOAD_PCT (5%) only
when all of them are off.
"""

import contextlib
import io
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

    def test_short_runtime_counts_after_the_blip_wait(self):
        self.extra = {"battery.runtime": str(pinut.MIN_RUNTIME_S - 300)}
        self.outage()
        self.play(pinut.BLIP_S - 5)
        self.assertEqual(self.asked, [])
        self.play(10)
        self.assertEqual(len(self.asked), 2)

    def test_short_runtime_must_hold(self):
        self.outage()
        self.play(pinut.BLIP_S + 10)
        self.extra = {"battery.runtime": str(pinut.MIN_RUNTIME_S - 300)}
        self.play(5)  # one reading, say a disk spinning up, is not enough
        self.extra = {"battery.runtime": str(pinut.MIN_RUNTIME_S + 600)}
        self.play(30)
        self.assertEqual(self.asked, [])
        self.extra = {"battery.runtime": str(pinut.MIN_RUNTIME_S - 300)}
        self.play(pinut.LOW_CONFIRM_S + 5)
        self.assertEqual(len(self.asked), 2)

    def test_min_runtime_zero_is_off(self):
        self.extra = {"battery.runtime": "0"}
        with mock.patch.object(pinut, "MIN_RUNTIME_S", 0):
            self.outage()
            self.play(pinut.MAX_ON_BATTERY_S - 5)
        self.assertEqual(self.asked, [])

    def test_early_server_is_asked_first_and_only_once(self):
        pinut.SERVERS[1]["early"] = True  # pc
        self.outage()
        start = self.t
        self.play(pinut.EARLY_SHUTDOWN_S - 5)
        self.assertEqual(self.asked, [])
        self.play(pinut.MAX_ON_BATTERY_S - pinut.EARLY_SHUTDOWN_S + 10)
        self.assertEqual(self.asked, [(start + pinut.EARLY_SHUTDOWN_S, "pc"),
                                      (start + pinut.MAX_ON_BATTERY_S, "nas")])

    def test_early_server_still_on_is_asked_again_by_the_shutdown(self):
        pinut.SERVERS[1]["early"] = True
        self.off_after["pc"] = None
        self.outage()
        self.play(pinut.MAX_ON_BATTERY_S + 10)
        self.assertEqual(sorted(n for _, n in self.asked), ["nas", "pc", "pc"])  # pc early, then again

    def test_mains_back_after_an_early_shutdown_stops_there(self):
        pinut.SERVERS[1]["early"] = True
        self.outage()
        self.play(pinut.EARLY_SHUTDOWN_S + 10)
        self.status = "OL"
        self.play(1200)
        self.assertEqual([n for _, n in self.asked], ["pc"])
        self.assertEqual(self.commands, [])

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

    def test_settings_problems(self):
        with mock.patch.object(pinut, "SERVERS", []):
            self.assertTrue(any("empty" in e for e in pinut.settings_problems()[0]))
        errors, _ = pinut.settings_problems()  # the test servers' keys do not exist
        self.assertTrue(any("nas: no key" in e for e in errors))
        dup = [{"name": "a", "host": "h", "user": "u", "key": "k"}] * 2
        with mock.patch.object(pinut, "SERVERS", dup):
            self.assertTrue(any("same name" in e for e in pinut.settings_problems()[0]))
        odd = [{"name": "a", "host": "h", "user": "u", "key": "k", "erly": True, "early": "yes"}]
        with mock.patch.object(pinut, "SERVERS", odd):
            errors, warnings = pinut.settings_problems()
        self.assertTrue(any("early should be true or false" in e for e in errors))
        self.assertTrue(any("erly is not a server setting" in w for w in warnings))


class ShutdownOne(unittest.TestCase):
    """--shutdown SERVER_NAME must never sound successful when ssh failed."""

    def play(self, returncode, err, key_exists=True):
        key = Path(tempfile.mkdtemp()) / "k"
        if key_exists:
            key.touch()
        server = {"name": "nas", "host": "10.0.0.1", "user": "root", "key": str(key)}
        p = mock.Mock(returncode=returncode)
        p.communicate.return_value = ("", err)
        out = io.StringIO()
        with mock.patch.object(pinut, "SERVERS", [server]), \
                mock.patch.object(pinut, "start_ssh", return_value=p) as start, \
                contextlib.redirect_stdout(out):
            code = pinut.shutdown_one("nas")
        return code, out.getvalue(), start.called

    def test_accepted(self):
        code, out, _ = self.play(0, "")
        self.assertEqual(code, 0)
        self.assertIn("OK:", out)

    def test_never_reached_the_server(self):
        code, out, _ = self.play(255, "ssh: connect to host 10.0.0.1 port 22: Connection timed out")
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)

    def test_connection_cut_by_the_shutdown_is_unsure(self):
        code, out, _ = self.play(255, "Connection to 10.0.0.1 closed by remote host.")
        self.assertEqual(code, 1)
        self.assertIn("UNSURE", out)

    def test_missing_key_sends_nothing(self):
        code, out, sent = self.play(0, "", key_exists=False)
        self.assertEqual(code, 2)
        self.assertIn("no key", out)
        self.assertFalse(sent)


class Config(unittest.TestCase):
    """config.toml over the SETTINGS in pinut.py."""

    def setUp(self):
        saved = {name: getattr(pinut, name) for name in pinut.SETTINGS}
        self.addCleanup(lambda: [setattr(pinut, n, v) for n, v in saved.items()])
        self.file = Path(tempfile.mkdtemp()) / "config.toml"
        patcher = mock.patch.object(pinut, "CONFIG_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, text, interactive=False):
        self.file.write_text(text)
        return pinut.load_config(interactive)

    def test_values_replace_the_settings_and_the_ups_name_follows(self):
        errors, warnings = self.load(
            'UPS_NAME = "apc-lr"\n'
            "DRY_RUN = false\n"
            "SAFE_LOAD_PCT = 7.5\n"
            "MIN_RUNTIME_S = 0   # off\n"
            'REBOOT_FILE = "~/reboots"\n'
            "[[SERVERS]]\n"
            'name = "omv"\n'
            'host = "192.168.1.40"\n'
            'user = "shutdownbot"\n'
            'key = "~/keys/omv"\n'
            "early = true\n")
        self.assertEqual((errors, warnings), ([], []))
        self.assertEqual(pinut.UPS, "apc-lr@localhost")
        self.assertEqual(pinut.DRIVER_SERVICE, "nut-driver@apc-lr.service")
        self.assertIs(pinut.DRY_RUN, False)
        self.assertEqual(pinut.SAFE_LOAD_PCT, 7.5)
        self.assertEqual(pinut.MIN_RUNTIME_S, 0)
        self.assertEqual(pinut.REBOOT_FILE, Path.home() / "reboots")
        self.assertEqual(pinut.SERVERS, [{"name": "omv", "host": "192.168.1.40", "user": "shutdownbot",
                                          "key": "~/keys/omv", "early": True}])

    def test_the_example_loads_cleanly(self):
        example = Path(pinut.__file__).with_name("config.example.toml").read_text()
        self.assertEqual(self.load(example), ([], []))
        self.assertEqual(pinut.DRIVER_SERVICE, "nut-driver@custom-ups-name.service")
        self.assertEqual(len(pinut.SERVERS), 2)
        self.assertTrue(any(s.get("early") for s in pinut.SERVERS))

    def test_ups_and_driver_service_set_directly_win(self):
        self.assertEqual(self.load('UPS_NAME = "a"\nUPS = "a@10.0.0.5"\nDRIVER_SERVICE = ""\n'), ([], []))
        self.assertEqual(pinut.UPS, "a@10.0.0.5")
        self.assertEqual(pinut.DRIVER_SERVICE, "")

    def test_a_name_that_is_not_a_setting_is_reported_and_not_used(self):
        errors, warnings = self.load("SAFE_LOAD_PC = 8\nsafe_load_pct = 8\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("write it SAFE_LOAD_PCT" in w for w in warnings))
        self.assertEqual(pinut.SAFE_LOAD_PCT, 5)
        self.assertFalse(hasattr(pinut, "SAFE_LOAD_PC"))

    def test_a_value_of_the_wrong_kind_is_an_error(self):
        errors, _ = self.load('SAFE_LOAD_PCT = "five"\nDRY_RUN = 1\nBLIP_S = true\nSERVERS = "omv"\n')
        self.assertEqual(len(errors), 4)
        self.assertEqual((pinut.SAFE_LOAD_PCT, pinut.DRY_RUN, pinut.BLIP_S), (5, True, 60))

    def test_a_setting_below_the_servers_is_an_error(self):
        errors, _ = self.load('[[SERVERS]]\nname = "omv"\nDRY_RUN = false\n')
        self.assertEqual(len(errors), 1)
        self.assertIn("DRY_RUN is inside the [[SERVERS]] for omv", errors[0])
        self.assertIs(pinut.DRY_RUN, True)

    def test_broken_toml_is_an_error(self):
        for text, quotes_hint in (('SERVERS = [\n  {name = "omv"\n', False),
                                  ("UPS_NAME = apc-lr\n", True), ("DRY_RUN = True\n", True)):
            errors, _ = self.load(text)
            self.assertEqual(len(errors), 1)
            self.assertIn(str(self.file), errors[0])
            self.assertEqual("quotes" in errors[0], quotes_hint, errors[0])

    def test_no_file_asks_in_a_terminal_and_warns_under_systemd(self):
        self.assertEqual(len(pinut.load_config(interactive=False)[1]), 1)
        with mock.patch("builtins.input", return_value="no"):
            self.assertEqual(len(pinut.load_config(interactive=True)[0]), 1)
        with mock.patch("builtins.input", return_value="yes"):
            self.assertEqual(pinut.load_config(interactive=True)[0], [])


class Calibrate(unittest.TestCase):
    """--calibrate: the warning, the CSV, and what the results say."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ssh = mock.Mock(returncode=0)
        self.ssh.poll.return_value = 0
        self.ssh.communicate.return_value = ("", "")
        self.status = "OL"
        servers = [{"name": "nas", "host": "10.0.0.1", "user": "root", "key": "~/k1"},
                   {"name": "pc", "host": "10.0.0.2", "user": "bot", "key": "~/k2"}]
        for name, value in {
            "SERVERS": servers,
            "CALIBRATE_S": 1,
            "CALIBRATION_DIR": self.dir,
            "read_ups": lambda: {"ups.status": self.status, "ups.load": "3"},
            "ping_all": lambda servers: {"nas": True, "pc": False},
            "start_ssh": mock.Mock(return_value=self.ssh),
        }.items():
            patcher = mock.patch.object(pinut, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def play(self, answer):
        out = io.StringIO()
        with mock.patch("builtins.input", return_value=answer), contextlib.redirect_stdout(out):
            code = pinut.calibrate()
        return code, out.getvalue()

    def test_nothing_is_sent_without_yes(self):
        code, out = self.play("y")
        self.assertEqual(code, 1)
        self.assertIn("for real", out)
        self.assertFalse(pinut.start_ssh.called)

    def test_only_on_mains(self):
        self.status = "OB DISCHRG"
        code, out = self.play("yes")
        self.assertIn("not on mains", out)
        self.assertFalse(pinut.start_ssh.called)

    def test_asks_every_server_and_saves_the_load_every_second(self):
        code, out = self.play("yes")
        self.assertEqual(pinut.start_ssh.call_count, 2)
        [csv_file] = self.dir.glob("calibration-*.csv")
        rows = csv_file.read_text().splitlines()
        self.assertEqual(rows[0], "time,load")
        self.assertEqual(len(rows), 1 + pinut.CALIBRATE_S + 1)
        self.assertTrue(all(r.endswith(",3") for r in rows[1:]))
        self.assertEqual(code, 1)  # nas still answers, pc never did
        self.assertIn("STILL ANSWERING", out)
        self.assertIn("never answered ping", out)

    def results(self, elapsed, last_reply, verdicts=None, loads=(3,) * 10):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            all_off = pinut.print_results(elapsed, last_reply, verdicts or {}, {}, list(loads))
        return all_off, out.getvalue()

    def test_results_use_the_last_reply(self):
        all_off, out = self.results(300, {"nas": 102.4, "pc": 15})
        self.assertTrue(all_off)
        self.assertIn("nas  stopped answering ping after 1m 42s", out)
        self.assertIn("Suggested SAFE_LOAD_PCT: 5", out)

    def test_a_reply_near_the_end_is_still_answering(self):
        all_off, out = self.results(300, {"nas": 295, "pc": 15},
                                    {"nas": "FAILED (ssh exit 255): the request never reached the server"})
        self.assertFalse(all_off)
        self.assertIn("STILL ANSWERING", out)
        self.assertIn("ssh FAILED", out)
        self.assertNotIn("Suggested", out)

    def test_load_above_safe_load_pct_is_flagged(self):
        all_off, out = self.results(300, {"nas": 10, "pc": 10}, loads=[9, 9, 11])
        self.assertIn("Average load, last 10 s: 9.7%", out)
        self.assertIn("Suggested SAFE_LOAD_PCT: 13", out)  # the highest, not the average, plus 2
        self.assertIn("could never prove them off", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
