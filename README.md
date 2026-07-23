# ARTIFACT X-II, player code

The music player half of ARTIFACT X-II: a Raspberry Pi feeding an ES9038Q2M DAC,
with a browser control surface built around a reel you grab to scrub and touch
to pause.

Put a finger on the reel while it is spinning and the music stops under your
hand. Lift it, and the song runs on.

This release covers the player only, which is phase 1 of the project. The haptic
motor firmware and the vinyl mode scratch engine are not included here.

## Read this before you install it

`artifactd` has no authentication. It listens on all interfaces on port 8080,
and anyone who can reach that port can upload files, delete files from disk,
rewrite tags, and, if you add the optional sudoers rule below, shut the machine
down.

That is a reasonable trade on a home network you control. It is not safe to
expose to the internet. Do not port forward it. If you want access from outside
your network, put it behind Tailscale, WireGuard, or an authenticating reverse
proxy.

## What is in here

```
artifactd.py          FastAPI service: MPD bridge, WebSocket state, uploads
static/index.html     the control surface, one self-contained file
requirements.txt      Python dependencies
```

## What you need

- Raspberry Pi 4, Raspberry Pi OS Lite 64-bit
- An ES9038Q2M I2S DAC HAT. Mine is sold as the InnoMaker HiFi DAC PRO and
  silkscreened SkyLark DAC, same board
- MPD

## DAC setup

In `/boot/firmware/config.txt`:

```
dtparam=audio=off
dtoverlay=vc4-kms-v3d,noaudio
dtoverlay=i-sabre-q2m
```

The `noaudio` on the KMS overlay removes the HDMI audio devices, so the DAC is
the only card and cannot lose a boot-order race.

Confirm with `aplay -l`. You should see one card named `DAC`.

## A driver bug worth knowing about

The i-sabre-q2m driver advertises S16_LE support, but in 16 bit mode the I2S bit
slots misalign on the ES9038Q2M, which wants 32 bit frames. The symptom is
recognisable music buried in harsh noise, with zero buffer underruns to explain
it. Anything that ships 24 or 32 bit audio never sees it, which is why MPD
sounded perfect while `speaker-test` and `aplay` did not.

The fix is an ALSA plug device that converts to S32_LE before the hardware. In
`~/.asoundrc`:

```
pcm.artifact32 {
    type plug
    slave {
        pcm "hw:DAC,0"
        format S32_LE
    }
}
```

To hear the difference, with headphones plugged into the HAT:

```
speaker-test -D plughw:DAC,0 -c 2 -r 48000 -t sine -f 440 -l 1
speaker-test -D artifact32   -c 2 -r 48000 -t sine -f 440 -l 1
```

The first is noise. The second is a clean tone. If you are running this DAC and
something sounds broken, start there. It cost me most of a night and seven wrong
suspects.

## MPD

In `/etc/mpd.conf`, address the card by name rather than index, so it survives
renumbering:

```
music_directory    "/home/YOURUSER/music"
user               "YOURUSER"

audio_output {
    type          "alsa"
    name          "ES9038Q2M"
    device        "hw:DAC,0"
    mixer_type    "software"
    auto_resample "no"
    auto_format   "no"
    auto_channels "no"
}
```

The three `auto_` lines are what keep playback bit perfect. Then:

```
sudo chown -R YOURUSER:YOURUSER /var/lib/mpd
sudo systemctl enable --now mpd
```

To confirm nothing is resampling, play a 24-bit 96kHz file and read what the
hardware is actually receiving:

```
cat /proc/asound/card0/pcm0p/sub0/hw_params
```

It should report `rate: 96000`. If it says 44100, something in the chain is
converting.

## artifactd

```
sudo apt install -y python3-venv git
git clone https://github.com/XerolandRegent/artifact-x2-player.git ~/artifact
mkdir -p ~/music

python3 -m venv ~/artifact/venv
~/artifact/venv/bin/pip install -r ~/artifact/requirements.txt
cd ~/artifact && ./venv/bin/python artifactd.py
```

Open `http://your-pi-address:8080`.

Music defaults to `~/music`, overridable with the `ARTIFACT_MUSIC_DIR`
environment variable.

### Running at boot

Create `/etc/systemd/system/artifactd.service`, substituting your username in
all four places:

```
[Unit]
Description=ARTIFACT control daemon
After=network-online.target mpd.service
Wants=mpd.service

[Service]
Type=simple
User=YOURUSER
WorkingDirectory=/home/YOURUSER/artifact
ExecStart=/home/YOURUSER/artifact/venv/bin/python /home/YOURUSER/artifact/artifactd.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now artifactd
```

The Pi now boots straight into being a music player.

### Optional: shutdown from the interface

Only add this if you accept the consequence, which is that anyone who can reach
port 8080 can power the machine off.

```
echo 'YOURUSER ALL=(ALL) NOPASSWD: /usr/bin/systemctl poweroff' | \
  sudo tee /etc/sudoers.d/artifact-poweroff
sudo chmod 440 /etc/sudoers.d/artifact-poweroff
```

## Using it

The reel spins while a track plays. Drag it to scrub, and hold it to pause under
your hand. The centre pad is play and pause, with a long press for stop. Up and
down move through the queue, left and right change screens. Triggers on the
right edge are momentary fast forward and rewind. The knob on the top edge is
volume, with a detent per step.

Drag audio files onto the device from a desktop browser to upload them, or use
the add music row in the system screen from a phone.

Settings include two chassis finishes, five screen palettes, a layout that
mirrors for left handed use, and live tuning for scrub ratio, trigger ramp and
detent step.

## Troubleshooting

No sound at all: check `aplay -l` lists the DAC, then `systemctl status mpd`.

Music buried in noise: you skipped the `.asoundrc` step above.

Service will not start: `journalctl -u artifactd -n 30`. Usually port 8080 is
still held by a copy running in the foreground.

Interface says offline: `artifactd` is running but MPD is not. Try
`sudo systemctl start mpd.socket mpd`.

## Standing on shoulders

The haptic architecture descends from Scott Bezek's SmartKnob, which solved
motor plus encoder plus closed loop torque control in the open, and did the
unglamorous work of finding a gimbal motor that does not cog.

Vinyl mode, not included in this release, runs on an engine descended from xwax
by way of SC1000 and ScratchTJ, who worked out how to drive a scratch engine
from a magnetic angle sensor instead of timecode vinyl.

Teenage Engineering taught everyone what digital audio hardware could feel like.

## Licence

Copyright 2026 XEROTECH LTD.

Licensed under the GNU Affero General Public License v3.0. See `LICENSE` for the
full text.

The vinyl mode engine derives from xwax, which is GPL-2.0, and will be published
separately under that licence rather than combined into this repository.

ARTIFACT and XEROTECH AI are trademarks of XEROTECH LTD. This licence covers the
source code only. It grants no rights to use the ARTIFACT or XEROTECH AI names,
logos, or product identity.

## Project

Build logs and the hardware side of the project are at
[https://hackaday.io/project/206236](https://hackaday.io/project/206236-artifact-x-ii)
