# ARTIFACT X-II, player code

The music player half of ARTIFACT X-II: a Raspberry Pi feeding an ES9038Q2M DAC,
with a browser control surface built around a reel you grab to scrub and touch
to pause.

This release covers the player only. The haptic motor firmware and the vinyl
mode engine integration are not included.

## Read this before you install it

`artifactd` has no authentication. It listens on all interfaces on port 8080,
and anyone who can reach that port can upload files, delete files from disk,
rewrite tags, and, if you add the optional sudoers rule below, shut the machine
down.

That is a reasonable trade on a home network you control. It is not safe to
expose to the internet. Do not port forward it. If you want access from
outside your network, put it behind Tailscale, WireGuard, or an authenticating
reverse proxy.

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

If you are running this DAC and something sounds broken, start there. It cost me
most of a night and seven wrong suspects.

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

## artifactd

```
mkdir -p ~/artifact/static ~/music
cp artifactd.py requirements.txt ~/artifact/
cp index.html ~/artifact/static/

python3 -m venv ~/artifact/venv
~/artifact/venv/bin/pip install -r ~/artifact/requirements.txt
cd ~/artifact && ./venv/bin/python artifactd.py
```

Open `http://your-pi-address:8080`.

Music paths default to `~/music` and can be overridden with the
`ARTIFACT_MUSIC_DIR` environment variable.

### Running at boot

Create `/etc/systemd/system/artifactd.service`, substituting your username:

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

Then `sudo systemctl enable --now artifactd`.

### Optional: shutdown from the interface

Only add this if you understand the consequence, which is that anyone who can
reach port 8080 can power the machine off.

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

## Standing on shoulders

The haptic architecture descends from Scott Bezek's SmartKnob, which solved
motor plus encoder plus closed loop torque control in the open, and did the
unglamorous work of finding a gimbal motor that does not cog.

Vinyl mode, not included in this release, runs on an engine descended from xwax
by way of SC1000 and ScratchTJ, who worked out how to drive a scratch engine
from a magnetic angle sensor instead of timecode vinyl.

## Licence

Add your chosen licence here before publishing.
