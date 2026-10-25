# PiNUT

Shut your servers down cleanly when the power goes out.

A Raspberry Pi plugged into your UPS by USB watches it through NUT (Network
UPS Tools). When the power has been out long enough, or the battery runs low,
it shuts each server down over SSH and proves they are really off. Then it
turns the UPS output off, so the battery is not drained for nothing.

This is the small version: one program (`pinut.py`, settings at the top),
its tests, and a systemd unit. No installer; the steps below are the install.

## How it works

Every 5 seconds the Pi reads the UPS. On mains, that is all it does.

1. **On battery: wait.** Outages shorter than `BLIP_S` (60 s) are ignored.
2. **Decide.** It shuts the servers down after `MAX_ON_BATTERY_S` (10 min)
   on battery, or sooner once the charge is down to `SHUTDOWN_CHARGE_PCT`
   (50%). Low battery (the UPS's own LB signal, or `LOW_CHARGE_PCT`) skips
   the 60 s wait, once it has held for 15 s.
3. **Shut down.** It asks every server at once, over SSH. A server still
   answering ping 5 minutes later is asked again, as often as it takes.
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
| `pinut.py` | The program. Settings at the top |
| `test_pinut.py` | Plays fake outages through the logic. No hardware needed |
| `pinut.service` | Starts it at boot |

## What you need

- A Raspberry Pi with Raspberry Pi OS (or another Debian), powered from the
  UPS, on the same network as the servers.
- A UPS that [NUT supports](https://networkupstools.org/stable-hcl.html),
  connected by USB, that accepts `load.off.delay` (step 2 checks).
- Admin access to each server, once.

## Setup

### 1. Copy it to the Pi

As your normal user:

```bash
sudo apt install nut
install -d -m 700 ~/pinut
install -m 755 pinut.py test_pinut.py ~/pinut/
python3 -c 'import secrets; print(secrets.token_hex(16))' > ~/pinut/upscmd.password
chmod 600 ~/pinut/upscmd.password
```

From here on, edit `~/pinut/pinut.py`, not the copy you downloaded.

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
upscmd -u upsctl -p "$(cat ~/pinut/upscmd.password)" -w custom-ups-name shutdown.stop   # must print OK
systemctl list-units 'nut-driver*'   # the driver's service name, for DRIVER_SERVICE
```

**Turn off NUT's own shutdown program.** It would shut the Pi down on low
battery, and can cut the UPS output while servers are still running:

```bash
sudo systemctl disable --now nut-monitor
```

### 3. Settings

`nano ~/pinut/pinut.py`. At the top:

- `UPS`: your UPS name, as `custom-ups-name@localhost`.
- `DRIVER_SERVICE`: the name the last check printed, usually
  `nut-driver@custom-ups-name.service`.
- `SERVERS`: one line per server: a name, its IP, the user PiNUT logs in
  as, and its key (`~/.ssh/pinut_<name>`, made in step 4).
- Leave `DRY_RUN = True` for now: it logs every decision and acts on none.
- `SAFE_LOAD_PCT` gets measured in step 6. The other defaults are a good start.

### 4. Set up each server

Each server gets its own key. **The server decides what the key can do:**
it is installed with a fixed shutdown command, so whatever the Pi sends,
that command runs, and a stolen key can only shut that one server down.

**a. Make the key**, on the Pi:

```bash
ssh-keygen -t ed25519 -N "" -C pinut-NAME -f ~/.ssh/pinut_NAME
cat ~/.ssh/pinut_NAME.pub     # you paste this on the server
hostname -I                   # this Pi's IP, for from= below
```

**b. Trust the server's host key.** On the server's own console, list its
fingerprints: `for k in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$k"; done`
(on Windows: `Get-ChildItem C:\ProgramData\ssh\ssh_host_*_key.pub | % { ssh-keygen -lf $_.FullName }`).
Then on the Pi, log in once with an ordinary admin account, and answer `yes`
only if the fingerprint matches:

```bash
ssh -o StrictHostKeyChecking=ask ADMIN_USER@SERVER_IP
```

PiNUT refuses any server whose host key is unknown or has changed.

**c. Install the key on the server**, for its kind. In each line, replace
`PI_IP` and `PUBLIC_KEY` (the whole `ssh-ed25519 ... pinut-NAME` line).

<details><summary><b>Ubuntu, Debian, OpenMediaVault</b></summary>

Create a user with no password and no admin rights. Ubuntu:
`sudo adduser --disabled-password --gecos "" shutdownbot`. OMV: create
`shutdownbot` in the Web UI, add it to the `_ssh` group only, then
`sudo passwd --lock shutdownbot`. (On OMV, never put the key in the Web UI's
*Public Keys* field: keys there get a full shell.)

```bash
H=$(getent passwd shutdownbot | cut -d: -f6)
sudo install -d -m 700 -o shutdownbot -g "$(id -gn shutdownbot)" "$H/.ssh"
echo 'from="PI_IP",restrict,command="/usr/bin/sudo -n /usr/bin/systemctl --no-block -i poweroff" PUBLIC_KEY' |
    sudo tee -a "$H/.ssh/authorized_keys" >/dev/null
sudo chown shutdownbot: "$H/.ssh/authorized_keys"
sudo chmod 600 "$H/.ssh/authorized_keys"

# Let it run exactly that one command as root
echo 'shutdownbot ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block -i poweroff' |
    sudo tee /etc/sudoers.d/shutdownbot-poweroff
sudo chmod 440 /etc/sudoers.d/shutdownbot-poweroff
sudo visudo -cf /etc/sudoers.d/shutdownbot-poweroff
```

`-i` powers off even when an app or update asks to wait: a clean shutdown
now beats a hard power cut later.
</details>

<details><summary><b>Unraid</b></summary>

Enable SSH in *Settings → Management Access*. Only `root` can log in, so the
fixed command is the whole protection. Use `powerdown`, never `poweroff`: it
stops the array first.

```bash
ls -ld /root/.ssh      # should point to /boot/config/ssh/root, so the key survives reboots
echo 'from="PI_IP",restrict,command="/usr/local/sbin/powerdown" PUBLIC_KEY' >> /root/.ssh/authorized_keys
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

**d. Test it for real**, at a time it is fine to turn the server off:

```bash
python3 ~/pinut/pinut.py --shutdown NAME
ping SERVER_IP          # until it stops answering
```

Turn it back on and test once more, to prove the setup survives a reboot.

### 5. Let PiNUT reboot the Pi and restart the driver

Exactly those two commands, nothing else. Use the `DRIVER_SERVICE` name from
step 3:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl restart nut-driver@custom-ups-name.service" |
    sudo tee /etc/sudoers.d/pinut
sudo chmod 440 /etc/sudoers.d/pinut
sudo visudo -cf /etc/sudoers.d/pinut
```

And install the service, without starting it yet:

```bash
sed -e "s|__USER__|$USER|" -e "s|__HOME__|$HOME|g" pinut.service |
    sudo tee /etc/systemd/system/pinut.service >/dev/null
sudo systemctl daemon-reload
```

### 6. Measure SAFE_LOAD_PCT

With **every server off** (after step 4's tests, for example), read the
load a few times:

```bash
upsc custom-ups-name ups.load
```

Set `SAFE_LOAD_PCT` to the highest reading plus 2. Too low, and PiNUT can
never prove the servers off, so it never cuts the output; too high, and a
server still on could pass for off. Each running server should add clearly
more than 2 to the load.

### 7. Test, in dry run

```bash
cd ~/pinut && python3 test_pinut.py     # must end with OK
sudo systemctl enable --now pinut
journalctl -u pinut -f
```

To watch an outage without waiting 10 minutes, set `MAX_ON_BATTERY_S = 90`,
`sudo systemctl restart pinut`, and unplug the UPS from the wall. The log
should go: `ON BATTERY`, `SHUTTING THE SERVERS DOWN`, `DRY RUN: would ask`,
then `waiting`. It stops there: nothing was really shut down, so nothing can
be proven off. Plug it back in (`MAINS BACK`) and set the value back.

### 8. Go live

Set `DRY_RUN = False`, then `sudo systemctl restart pinut`.

The first real outage is worth doing on purpose, at a quiet time: unplug the
UPS, watch the log until the output is cut, then plug it back in and check
the output comes back by itself. The log shows how long each server took
("stopped answering ping ... after the shutdown began"): if the slowest one
plus 2 minutes does not fit well inside your UPS's runtime after
`MAX_ON_BATTERY_S`, lower `MAX_ON_BATTERY_S`.

## Day to day

- **Log:** `journalctl -u pinut -f`. It logs events only.
- **Before working on NUT, stop PiNUT.** A stopped driver looks exactly like
  a lost USB connection, and the Pi reboots 3 minutes later:
  `sudo systemctl stop pinut`, work, `sudo systemctl start pinut`.
- **After changing a setting:** `sudo systemctl restart pinut`. Settings
  errors show in the log, and it does not start until they are fixed.

## Uninstall

```bash
sudo systemctl disable --now pinut
sudo rm /etc/systemd/system/pinut.service /etc/sudoers.d/pinut
sudo systemctl daemon-reload
rm -r ~/pinut ~/.ssh/pinut_*
sudo systemctl enable --now nut-monitor   # only if you had it on before
```

On each server, delete the key's line (it ends in `pinut-NAME`); on Linux
also `/etc/sudoers.d/shutdownbot-poweroff` and the user; on Windows the
`Match User shutdownbot` block, then `Restart-Service sshd` and
`Remove-LocalUser shutdownbot`.

## What it does not do

- **Start servers again.** They stay off after an outage until you start
  them.
- **Shut down in an order** (storage last, say). Everything goes at once.
- **Cancel the cut if the mains returns** during the UPS's few-second delay.
  The servers are off by then anyway.
- **Use the UPS's runtime estimate.** Many report it badly; charge and time
  on battery are used instead.
