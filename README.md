# PiNUT

Shut your servers down cleanly when the power goes out.

A Raspberry Pi plugged into your UPS by USB watches it through NUT (Network
UPS Tools). When the power has been out long enough, or the battery runs low,
it shuts each server down over SSH and proves they are really off. Then it
turns the UPS output off, so the battery is not drained for nothing.

This is the small version: one program (`pinut.py`), a short settings file,
its tests, and a systemd unit. No installer; the steps below are the install.

## How it works

Every 5 seconds the Pi reads the UPS. On mains, that is all it does.

1. **On battery: wait.** Outages shorter than `BLIP_S` (60 s) are ignored.
2. **Decide.** It shuts the servers down after `MAX_ON_BATTERY_S` (10 min)
   on battery, or sooner: once the charge is down to `SHUTDOWN_CHARGE_PCT`
   (50%), or once the UPS's runtime estimate has been below `MIN_RUNTIME_S`
   (10 min) for 15 s. Low battery (the UPS's own LB signal, or
   `LOW_CHARGE_PCT`) skips the 60 s wait, once it has held for 15 s.
3. **Shut down.** It asks every server at once, over SSH. A server still
   answering ping 5 minutes later is asked again, as often as it takes.
   Servers marked `early` go first, `EARLY_SHUTDOWN_S` (5 min) into the
   outage: shutting down a power-hungry machine you can spare makes the
   battery last longer for the rest.
4. **Prove they are off.** No server answers ping **and** the UPS load is
   at or below `SAFE_LOAD_PCT`, without a break, for `PROOF_S` (90 s). It
   checks again every 5 s, so a server that comes back starts the proof over.
5. **Cut the output.** It sends `load.off.delay`: the UPS turns its output
   off after its own short delay, the Pi included. When the mains returns,
   most UPSes turn the output back on and the Pi starts again. The servers
   stay off until you start them.

Doubt always means "don't cut": a ping that failed to run, a missing load
reading or an unclear UPS status never count as "off". The output is cut
only when every server is proven off, never on a timer.

**Lost contact with the UPS** (USB unplugged, driver stuck): after 60 s it
restarts the NUT driver; after 3 minutes it reboots the Pi, at most twice a
day. If the UPS was on battery when contact was lost, it shuts the servers
down after 60 s, but cannot cut the output: that needs fresh UPS readings.

**If the Pi restarts mid-outage**, it finds the UPS on battery at its first
reading and shuts the servers down straight away. It keeps nothing on disk
except when it last rebooted the Pi.

## Files

| File | |
|---|---|
| `pinut.py` | The program. Every setting and its default is at the top |
| `config.example.toml` | The settings you change, to copy to `config.toml` (step 3) |
| `test_pinut.py` | Plays fake outages through the logic. No hardware needed |
| `pinut.service` | Starts it at boot |
| `.gitignore` | Keeps your `config.toml`, the NUT password, the reboot count and calibration files out of git |

## What you need

- A Raspberry Pi with Raspberry Pi OS 12 (Bookworm) or newer, or another
  Debian with Python 3.11 or newer, powered from the UPS, on the same
  network as the servers. `python3 --version` shows yours.
- A UPS that [NUT supports](https://networkupstools.org/stable-hcl.html),
  connected by USB, that accepts `load.off.delay` (step 2 checks).
- Admin access to each server, once.

## Setup

### 1. Download it to the Pi

As your normal user:

```bash
sudo apt install nut git
git clone https://github.com/YOUR-USERNAME/pinut.git ~/pinut
cd ~/pinut
python3 -c 'import secrets; print(secrets.token_hex(16))' > upscmd.password
chmod 600 upscmd.password
```

PiNUT runs straight from this folder, and it must be `~/pinut`: the service
and the settings look for it there. Ending the `git clone` line with
`~/pinut` puts it there whatever folder you run it from.

The password is for NUT, in step 2. `.gitignore` keeps it, your settings and the files PiNUT writes
later out of git.

### 2. Set up NUT

NUT has three parts: a **driver** that talks to the UPS over USB, a
**server** (`upsd`) that shares what the driver reads, and **clients** like
`upsc` that ask the server.

**Find the UPS:** `sudo nut-scanner -U` prints a block with `driver`,
`vendorid`, `productid` and `serial`.

**Name it**, at the end of `/etc/nut/ups.conf`, with the values it printed:

```ini
[custom-ups-name]
    driver = usbhid-ups
    port = auto
    vendorid = 051d
    productid = 0002
    serial = YOUR_UPS_SERIAL
```

The name in square brackets is your choice (letters, numbers, `-`, `_`).
Every command below uses it.

**IMPORTANT!**

**Copy only these five lines** from what `nut-scanner` printed: `driver`,
`port`, `vendorid`, `productid` and `serial`. Leave out everything else:

- `bus`, `device` and `busport` describe where the UPS sits on the USB bus
  *right now*. `device` changes when the UPS is replugged, and `bus` and
  `busport` when it goes into another USB port. A copied one can then stop
  matching, and the driver no longer finds the UPS. Without them, the
  driver finds the UPS by its IDs and serial, wherever it is plugged in.
- `product` and `vendor` are not needed, and `product` contains the
  firmware version (`FW:...`), so it would stop matching after a firmware
  update. The `###NOTMATCHED-YET###` line is a note from `nut-scanner`,
  not a setting.

<details><summary><b>The driver says <code>libusb1: Could not open any HID devices: insufficient permissions on everything</code></b></summary>

The `nut` user, which the driver runs as, is not allowed to open the UPS's
USB device. That permission comes from a rule NUT installs, and the rule
only applies when the device is plugged in. So if the UPS was already
connected when you installed NUT, the rule never ran for it.

Unplug the UPS's USB cable and plug it back in (or reboot the Pi), then
restart NUT with the commands below. To check the permission:

```bash
lsusb                          # e.g. "Bus 003 Device 002: ID 051d:0002 American Power Conversion..."
ls -l /dev/bus/usb/003/002     # use your Bus and Device numbers; the group must be nut
```

You see this message where the driver logs: `journalctl -u 'nut-driver@*'`.
</details>

**In `/etc/nut/nut.conf`:** `MODE=standalone`.

**The account PiNUT sends commands with**, at the end of
`/etc/nut/upsd.users`. Paste the password from `cat ~/pinut/upscmd.password`:

```ini
[upsctl]
    password = PASTE_THE_PASSWORD_HERE
    instcmds = load.off.delay
    instcmds = shutdown.stop
```

`load.off.delay` is the cut. `shutdown.stop` is only there for the harmless
password test below; PiNUT never sends it.

**Restart NUT** (the driver too):

```bash
sudo upsdrvsvcctl stop custom-ups-name
sudo upsdrvsvcctl resync
sudo systemctl restart nut-server
```

**Check it:**

```bash
upsc custom-ups-name ups.status      # OL = on mains, OB = on battery
upsc custom-ups-name ups.load
upscmd -l custom-ups-name            # must list load.off.delay
upscmd -u upsctl -p "$(cat ~/pinut/upscmd.password)" -w custom-ups-name shutdown.stop   # must print SUCCESS
systemctl list-units 'nut-driver*'   # must list nut-driver@custom-ups-name.service
```

**Turn off NUT's own shutdown program.** It would shut the Pi down on low
battery, and can cut the UPS output while servers are still running:

```bash
sudo systemctl disable --now nut-monitor
```

### 3. Settings

Your settings go in `~/pinut/config.toml`. Start from the example:

```bash
cd ~/pinut
cp config.example.toml config.toml
nano config.toml
```

- `UPS_NAME`: the name in square brackets from step 2. PiNUT works out the
  rest from it: `custom-ups-name@localhost` to read the UPS, and
  `nut-driver@custom-ups-name.service` to restart the driver.
- `SERVERS`: one `[[SERVERS]]` block per server, at the end of the file: a
  name, its IP, the user PiNUT logs in as, and its key (`~/.ssh/pinut_SERVER_NAME`, made in step 4). You fill
  these in as you set each server up in step 4.
- Leave `DRY_RUN = true` for now: it logs every decision and acts on none.
- `SAFE_LOAD_PCT` gets measured in step 6.
- `MAX_ON_BATTERY_S` and `MIN_RUNTIME_S` depend on how long this UPS lasts
  with your servers on it. The defaults are a good start. If this UPS's
  runtime estimate (`upsc custom-ups-name battery.runtime`, in seconds) is
  far from what it really lasts, turn that trigger off with
  `MIN_RUNTIME_S = 0`.

The file's rules (it is [TOML](https://toml.io)):

- Text goes in `"quotes"`; `true`, `false` (lowercase) and numbers do not.
- `#` starts a comment.
- Every line below a `[[SERVERS]]` belongs to that server. Put settings above
  the first one; PiNUT stops with an error if it finds one below.

Every setting at the top of `pinut.py` can go in `config.toml`, with the same
name, and the value in `config.toml` wins. A name that is not a setting is
reported in the log and not used; a value of the wrong kind (text where a
number belongs, say) stops PiNUT with an error saying which. git ignores
`config.toml`, so an update never touches your settings.

<details><summary><b>Keeping the settings in <code>pinut.py</code> instead</b></summary>

With no `config.toml`, PiNUT uses the settings at the top of `pinut.py` as
they are. Run by hand, it asks first; as a service, it logs a warning and
carries on. An update can then conflict with your edits (see *Updating*).
</details>

### 4. Set up each server

Each server gets its own key. **The server decides what the key can do:**
it is installed with a fixed shutdown command, so whatever the Pi sends,
that command runs, and a stolen key can only shut that one server down.

Do steps a to d once per server. In the commands, replace these with your
own values:

| Placeholder | What it is | Example |
|---|---|---|
| `SERVER_NAME` | The server's `name` in `SERVERS` (step 3). Short, no spaces | `omv` |
| `SERVER_IP` | The server's IP address | `192.168.1.40` |
| `ADMIN_USER` | Your usual admin account on the server, the one you log in with | `admin` |
| `PUBLIC_KEY` | The line `cat` prints in step a | `ssh-ed25519 AAAAC3Nz...Qz pinut-omv` |

**a. Make the key**, on the Pi:

```bash
ssh-keygen -t ed25519 -N "" -C pinut-SERVER_NAME -f ~/.ssh/pinut_SERVER_NAME
cat ~/.ssh/pinut_SERVER_NAME.pub     # prints the PUBLIC_KEY you paste into the commands in step c
```

For a server named `omv`, that is `ssh-keygen ... -C pinut-omv -f ~/.ssh/pinut_omv`
and `cat ~/.ssh/pinut_omv.pub`. It makes two files:

- `~/.ssh/pinut_omv`: the **private** key. It never leaves the Pi. This is
  the `key` in `SERVERS`.
- `~/.ssh/pinut_omv.pub`: the **public** key, one line of text like
  `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx...Qz pinut-omv`.

You don't copy any file to the server (no `scp`). You copy that one line from
the Pi's terminal and paste it into step c's command, in place of
`PUBLIC_KEY`. The command writes it into the right file on the server.

**b. Trust the server's host key.** This makes sure the Pi talks to your
real server and not something pretending to be it. On the server's own
console (or an SSH session you already trust), list its fingerprints:

```bash
for k in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$k"; done
```

On Windows: `Get-ChildItem C:\ProgramData\ssh\ssh_host_*_key.pub | % { ssh-keygen -lf $_.FullName }`.

It prints several lines, one per key type (ED25519, ECDSA, RSA). That is
normal. Each looks like `256 SHA256:abc123... root@omv (ED25519)`. There is
nothing to copy from them; you only compare them by eye.

Then, on the Pi, log in once with your admin account:

```bash
ssh -o StrictHostKeyChecking=ask ADMIN_USER@SERVER_IP
```

- **It asks "Are you sure you want to continue connecting?"**: answer `yes`
  only if the fingerprint it shows matches one of the server's lines. Then
  log in with your password and `exit`.
- **It goes straight to the password prompt, no question**: the Pi already
  trusts this server (you connected to it before). Log in and `exit`, then
  check that what the Pi trusts is the real key:

  ```bash
  ssh-keygen -l -F SERVER_IP
  ```

  Find the line with the same type (ED25519, say) in both lists. The
  `SHA256:...` text must be identical. If it differs, or the command prints
  nothing, stop and find out why before going on.

PiNUT refuses any server whose host key is unknown or has changed.

**c. Install the key on the server**, for its kind. In each command,
replace `PUBLIC_KEY` with the whole line from step a, from `ssh-ed25519` to
`pinut-SERVER_NAME`, inside the single quotes and on one line.

<details><summary><b>Ubuntu, Debian, OpenMediaVault</b></summary>

**Create the user `shutdownbot`**, the account PiNUT logs in as. It gets no
usable password and no admin rights.

- **Ubuntu, Debian:**

  ```bash
  sudo adduser --disabled-password --gecos "" shutdownbot
  ```

- **OpenMediaVault:** OMV insists on a password, so give it one and then lock
  it. A locked password turns off password login, but key login still works.
  Either way works:

  - In the Web UI: *Users → Create* `shutdownbot` with any long random
    password (you won't need it again), and add it to the `_ssh` group only,
    not `sudo`. Then in the OMV terminal: `sudo passwd --lock shutdownbot`.
  - Or all in the OMV terminal:

    ```bash
    sudo useradd -m -s /bin/bash -G _ssh shutdownbot
    sudo passwd --lock shutdownbot
    ```

  OMV's SSH server only lets in root and members of `_ssh`, which is why the
  group matters. Never put the key in the Web UI's *Public Keys* field: keys
  there get a full shell.

**Check the user**, before going on:

```bash
getent passwd shutdownbot   # the last field must be a real shell: /bin/bash or /usr/bin/sh
id shutdownbot              # on OMV, must list _ssh
```

If the last field is `/usr/sbin/nologin` or `/bin/false`, the fixed command
cannot run and the shutdown fails: `sudo usermod -s /bin/bash shutdownbot`.

**Install the key and allow the shutdown.** Paste your `PUBLIC_KEY` into
the `echo` line first:

```bash
H=$(getent passwd shutdownbot | cut -d: -f6)
sudo install -d -m 700 -o shutdownbot -g "$(id -gn shutdownbot)" "$H/.ssh"
echo 'restrict,command="/usr/bin/sudo -n /usr/bin/systemctl --no-block -i poweroff" PUBLIC_KEY' |
    sudo tee -a "$H/.ssh/authorized_keys" >/dev/null
sudo chown shutdownbot: "$H/.ssh/authorized_keys"
sudo chmod 600 "$H/.ssh/authorized_keys"

# Let it run exactly that one command as root
echo 'shutdownbot ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block -i poweroff' |
    sudo tee /etc/sudoers.d/shutdownbot-poweroff
sudo chmod 440 /etc/sudoers.d/shutdownbot-poweroff
sudo visudo -cf /etc/sudoers.d/shutdownbot-poweroff
```

With the key pasted in, the `echo` line looks like this:

```bash
echo 'restrict,command="/usr/bin/sudo -n /usr/bin/systemctl --no-block -i poweroff" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx...Qz pinut-omv' |
```

Check that the key landed as **one** line (a terminal can wrap a long line
when you copy it, and then the key does not work):

```bash
sudo cat "$H/.ssh/authorized_keys"
```

If `visudo` says "command not found", install sudo first: `sudo apt install sudo`.

`-i` powers off even when an app or update asks to wait: a clean shutdown
now beats a hard power cut later.
</details>

<details><summary><b>Unraid</b></summary>

Enable SSH in *Settings → Management Access*. Only `root` can log in, so the
fixed command is the whole protection. Use `powerdown`, never `poweroff`: it
stops the array first.

```bash
ls -ld /root/.ssh      # should point to /boot/config/ssh/root, so the key survives reboots
echo 'restrict,command="/usr/local/sbin/powerdown" PUBLIC_KEY' >> /root/.ssh/authorized_keys
cat /root/.ssh/authorized_keys     # the key must be on one line
```

`powerdown` keeps the SSH session open while the array stops, so the log
shows the request ending with an error when the shutdown cuts it. That is
normal: PiNUT goes by ping and load, not by what ssh reports.
</details>

<details><summary><b>Windows 10/11</b></summary>

In PowerShell as Administrator. Keep `shutdownbot` a standard user: Windows
reads administrators' keys from a different file.

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service sshd -StartupType Automatic
$pw = Read-Host "temporary password" -AsSecureString
New-LocalUser shutdownbot -Password $pw
Set-LocalUser shutdownbot -PasswordNeverExpires $true
runas /user:.\shutdownbot cmd      # logs in once so its profile exists; close it

$d = 'C:\Users\shutdownbot\.ssh'
New-Item -ItemType Directory -Force -Path $d | Out-Null
Set-Content -Path "$d\authorized_keys" -Value 'PUBLIC_KEY' -Encoding ascii
icacls $d /inheritance:r /grant:r "shutdownbot:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"
icacls "$d\authorized_keys" /inheritance:r /grant:r "shutdownbot:F" "SYSTEM:F" "Administrators:F"
```

Append this at the **very end** of `C:\ProgramData\ssh\sshd_config`
(`PermitTTY no` must stay, or Windows can hand out a shell):

```text
Match User shutdownbot
    AuthenticationMethods publickey
    PasswordAuthentication no
    PermitTTY no
    DisableForwarding yes
    ForceCommand C:\Windows\System32\shutdown.exe /s /f /t 0
```

Then `& "$env:WINDIR\System32\OpenSSH\sshd.exe" -t; if ($LASTEXITCODE -eq 0) { Restart-Service sshd }`.
Windows blocks ping by default; PiNUT needs it, so allow *File and Printer
Sharing (Echo Request - ICMPv4-In)* in the firewall.
</details>

<details><summary><b>Optional: accept the key only from this Pi (<code>from=</code>)</b></summary>

On Linux and Unraid you can start the key line with `from="PI_IP",`, so the
server refuses the key from any other address:

```text
from="192.168.1.20",restrict,command="..." ssh-ed25519 AAAA... pinut-omv
```

`hostname -I` on the Pi shows its IP. **Only do this if the Pi's address
never changes**: give it a fixed address in your router (a "DHCP
reservation"), or allow your whole network with `from="192.168.1.0/24"`.
Otherwise, the day the Pi gets a new address the servers refuse the key, and
in the next outage nothing shuts down. Don't use the Pi's hostname here: it
usually never matches.
</details>

**d. Test it for real**, at a time it is fine to turn the server off. First
add a block for it at the end of `~/pinut/config.toml`, for example:

```toml
[[SERVERS]]
name = "omv"
host = "192.168.1.40"
user = "shutdownbot"
key = "~/.ssh/pinut_omv"
```

Add `early = true` to a power-hungry machine you can spare, like a
workstation, to shut it down first in an outage.

Then, on the Pi:

```bash
python3 ~/pinut/pinut.py --shutdown SERVER_NAME
ping SERVER_IP          # until it stops answering; Ctrl-C to stop
```

The last line it prints is one of these:

- `OK`: the server accepted the request.
- `FAILED`: the request never reached the server, and the error above it says
  why. For example, a timeout means nothing answered on port 22.
- `UNSURE`: the connection dropped. A shutdown that cuts its own connection
  ends like this too, so watch the ping.

Turn it back on and test once more, to prove the setup survives a reboot.

### 5. Let PiNUT reboot the Pi and restart the driver

Exactly those two commands, nothing else. Put your UPS name from step 2 in
the driver's service name:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl restart nut-driver@custom-ups-name.service" |
    sudo tee /etc/sudoers.d/pinut
sudo chmod 440 /etc/sudoers.d/pinut
sudo visudo -cf /etc/sudoers.d/pinut
```

And install the service, without starting it yet:

```bash
cd ~/pinut
sed -e "s|__USER__|$USER|" -e "s|__HOME__|$HOME|g" pinut.service |
    sudo tee /etc/systemd/system/pinut.service >/dev/null
sudo systemctl daemon-reload
```

### 6. Calibrate: time the shutdowns and measure SAFE_LOAD_PCT

On mains power, at a time it is fine for **every** server to go off:

```bash
python3 ~/pinut/pinut.py --calibrate
```

It warns you first and only goes ahead once you type `yes`. Then it asks
every server to shut down at once, for real (`DRY_RUN` does not apply; the
UPS output is never cut). For `CALIBRATE_S` (5 minutes) it records the UPS
load every second and keeps pinging the servers. Let it run to the end.
Then it prints something like:

```text
RESULTS after 5m 00s:
  unraid  stopped answering ping after 2m 13s
  omv     stopped answering ping after 0m 41s

Average load, last 10 s: 3.0%
Suggested SAFE_LOAD_PCT: 5 (the highest of those, plus 2). Yours is 5
Load every second: /home/pi/pinut/calibration-20261027-141502.csv
```

- **Each time** is from the request to the server's last ping reply. The
  slowest one is how much battery your shutdown needs (step 8).
- **Set `SAFE_LOAD_PCT`** in `config.toml` to the suggested value. Too low,
  and PiNUT can never prove the servers off, so it never cuts the output;
  too high, and a server still on could pass for off. Each running server
  should add clearly more than 2 to the load.
- **`STILL ANSWERING`**: that server took longer than the test, or never
  shut down; the ssh result under it says which. Raise `CALIBRATE_S` in
  `config.toml` and run it again.
- **`never answered ping`**: a wrong `host`, or a firewall blocking ping.
  PiNUT relies on ping to tell when a server is off, so fix this first.
- **The CSV** has a `time,load` row for every second, to open in a
  spreadsheet. The load may change in steps of a few seconds: that is how
  often the UPS driver refreshes it.

Then turn the servers back on.

### 7. Test, in dry run

```bash
cd ~/pinut && python3 test_pinut.py     # must end with OK
sudo systemctl enable --now pinut
journalctl -u pinut -f
```

The log's first line says where the settings came from (`settings from
.../config.toml`).

To watch an outage without waiting 10 minutes, add `MAX_ON_BATTERY_S = 90`
to `config.toml` (above the servers), `sudo systemctl restart pinut`, and unplug the UPS from the
wall. The log should go: `ON BATTERY`, `SHUTTING THE SERVERS DOWN`,
`DRY RUN: would ask`, then `waiting`. (`SHUTTING THE SERVERS DOWN` can come
sooner, with the runtime as its reason, if the UPS's estimate is below
`MIN_RUNTIME_S`.) It stops there: nothing was really shut down, so nothing
can be proven off. Plug it back in (`MAINS BACK`) and set the value back.

### 8. Go live

Set `DRY_RUN = false` in `config.toml`, then `sudo systemctl restart pinut`.

Check that the shutdown fits in the battery: the slowest time from step 6,
plus 2 minutes, must fit well inside what is left of your UPS's runtime
when the shutdown starts. If it doesn't, lower `MAX_ON_BATTERY_S`, raise
`MIN_RUNTIME_S` or `SHUTDOWN_CHARGE_PCT`, or mark a power-hungry server
`early`.

The first real outage is worth doing on purpose, at a quiet time: unplug the
UPS, watch the log until the output is cut, then plug it back in and check
the output comes back by itself.

## Day to day

- **Log:** `journalctl -u pinut -f`. It logs events only.
- **Before working on NUT, stop PiNUT.** A stopped driver looks exactly like
  a lost USB connection, and the Pi reboots 3 minutes later:
  `sudo systemctl stop pinut`, work, `sudo systemctl start pinut`.
- **After changing a setting:** `sudo systemctl restart pinut`. Settings
  errors show in the log, and it does not start until they are fixed.
  Warnings (a name that is not a setting, say) show too, but it starts.
- **Updating.** Your settings are in `config.toml`, which git leaves alone:

  ```bash
  cd ~/pinut
  git pull
  python3 test_pinut.py && sudo systemctl restart pinut
  ```

  If you kept your settings in `pinut.py` instead, set your edits aside
  while git updates the file: `git stash && git pull && git stash pop`. If
  `git stash pop` reports a conflict, open `pinut.py`, keep your values,
  delete the `<<<<<<<`, `=======` and `>>>>>>>` lines, then `git stash drop`.

## Uninstall

```bash
sudo systemctl disable --now pinut
sudo rm /etc/systemd/system/pinut.service /etc/sudoers.d/pinut
sudo systemctl daemon-reload
rm -r ~/pinut ~/.ssh/pinut_*
sudo systemctl enable --now nut-monitor   # only if you had it on before
```

On each server, delete the key's line (it ends in `pinut-SERVER_NAME`); on Linux
also `/etc/sudoers.d/shutdownbot-poweroff` and the user; on Windows the
`Match User shutdownbot` block, then `Restart-Service sshd` and
`Remove-LocalUser shutdownbot`.

## What it does not do

- **Start servers again.** They stay off after an outage until you start
  them.
- **Shut down in an order** (storage last, say). Everything goes at once,
  apart from the `early` servers, which go first to save battery.
- **Start an `early` server again** if the mains returns before the others
  shut down. It stays off like the rest.
- **Cancel the cut if the mains returns** during the UPS's few-second delay.
  The servers are off by then anyway.
