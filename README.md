# Si4463 Packet Decoder — Raspberry Pi

Telemetry decoder for the Si4463 2GFSK link, running on a Raspberry Pi 5
(Debian Bookworm) with an RTL-SDR Blog V3. It takes the signal as complex
baseband straight off the dongle, then prints and logs each `SensorPacket_t` as
it arrives.

The whole signal path is **GNU Radio**, run headless:

```
osmosdr.source  ->  freq_xlating_fir_filter_ccf (firdes.low_pass)  ->  Si4463Sink
```

Everything comes from apt (`gnuradio`, `gr-osmosdr`, `rtl-sdr`). No pip, no
virtualenv.

## Install

On the Pi:

```bash
git clone https://github.com/nrp2909/stpl-si4463-decoder.git
cd stpl-si4463-decoder
./install.sh
```

Then **unplug and replug the RTL-SDR** (see below for why), and run it:

```bash
./si4463_rt.py
```

That is the whole installation. `git` is already on Raspberry Pi OS, the repo
is public so there are no credentials to set up, and the clone is about 3 MB.
The apt step pulls in GNU Radio, which is roughly 1.5 GB with its
dependencies, so the first run takes a while.
The executable bits are recorded in the repo, so `./install.sh` runs directly
without `bash` in front of it.

### What `install.sh` does

It is idempotent — re-run it any time. In order, it:

1. installs `gnuradio`, `gr-osmosdr` and `rtl-sdr` from apt,
2. blacklists the DVB-T kernel driver and unloads it,
3. checks udev permissions and adds you to `plugdev` if needed,
4. verifies the dongle with `rtl_test -t`,
5. generates the systemd unit for your account and path,
6. checks GNU Radio and gr-osmosdr import, runs the decoder test suite and
   decodes the bundled reference capture through the GNU Radio chain,
7. compiles the diagnostic flowgraph with `grcc`,
8. benchmarks the chain on your Pi and recommends a sample rate.

Nothing it installs comes from pip, which also sidesteps Bookworm's PEP 668
"externally-managed-environment" refusal — and apt's GNU Radio is built against
the system VOLK, which is what makes the filter fast on ARM.

### Two things that catch people

**Replug the dongle.** This is the one step that is easy to skip and breaks
everything. An RTL-SDR announces itself as a TV receiver, so the kernel binds
`dvb_usb_rtl28xxu` to it on plug-in and gr-osmosdr then cannot claim the USB
interface. It surfaces as "no SDR found" or `usb_claim_interface error -6`.
`install.sh` blacklists the driver and unloads it, but the dongle has to be
re-enumerated for that to take effect.

**Log out and back in** if `install.sh` said it added you to `plugdev`. Group
membership does not apply to a shell that already exists.

### Update

```bash
git pull
```

`logs/` and the generated `si4463-decoder.service` are in `.gitignore`, so a
pull never collides with your packet logs or your local unit file.

## Run

```bash
./si4463_rt.py                                  # live, 435.000 MHz
./si4463_rt.py --rate 250e3 --lo-offset 50e3    # low-CPU mode
./si4463_rt.py --list-devices
./si4463_rt.py --wav test_current_layout.wav    # replay, no radio needed
```

Headless, at boot:

```bash
sudo cp si4463-decoder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now si4463-decoder
journalctl -u si4463-decoder -f
```

Packets land in `logs/packets_<date>_<time>.csv`, one row each, flushed as they
arrive — so the file is a live record rather than something that only exists
once the program stops. `--csv <path>` moves it, `--no-csv` turns it off.

## Checking it works without a transmitter

Two checks, in increasing strength, both runnable on the Pi with no radio
attached:

```bash
./si4463_selftest.py         # the decoder, synthesised frames buried in noise
./si4463_rt.py --wav test_current_layout.wav
```

The second is the real one: a genuine 3.9 s recording containing six known
packets, through the identical GNU Radio flowgraph the live path uses — only
the osmosdr source is swapped for a `wavfile_source`. It should report
**6 packets from 6 bursts, 0 with bit errors, 0 lost by counter**, with counters
0–5 and ~18.3 dB SNR.

## Reliability: what the decoder watches for

Three failures look exactly like a quiet band in the packet log, so the live
decoder measures each one while the flowgraph runs:

| Failure | How it is caught | What happens |
|---------|------------------|--------------|
| **The Pi falls behind** and librtlsdr's buffers overflow | Samples reaching the decoder are counted against the wall clock every 10 s | A shortfall over 5% is printed with its size, and totalled in the final summary |
| **The dongle stops streaming** — pulled, browned out, or a wedged USB transfer | No samples for `--stall-timeout` seconds (default 10) | The decoder exits non-zero; the systemd unit's `Restart=always` reopens the device 5 s later |
| **A decoder exception** on a malformed burst | Caught inside the GNU Radio block's `work()` | The traceback is logged (at most once a minute) and the stream carries on, instead of the block's thread dying silently |

`--status` also prints the process's CPU load (every GNU Radio thread) per
second of signal, and warns once if it passes 80%.

## Seeing the signal: the GNU Radio flowgraph

`si4463_rt.py` tells you what decoded. When nothing decodes, that is exactly
the information you do not have — and "no packets" looks identical whether the
transmitter is off, the gain is wrong, the tuning is off, or the Pi is dropping
samples. `si4463_rpi.grc` is the answer to that: the same chain with three Qt
sinks attached, so you can look at the signal.

```bash
./si4463_grc.sh
```

Three views, each answering one question:

| View | Question it answers |
|------|---------------------|
| **Wideband spectrum** (1.2 MHz, off the dongle) | Is the burst there at all, and is the DC spike clear of it? The burst should sit `lo_offset` below centre with the spike at centre. |
| **Waterfall** (200 kHz, after the filter) | Are bursts arriving on time? They appear as regular stripes ~1.12 s apart, centred at 0 Hz. A gap is a lost packet; drift off centre is tuner error. |
| **Burst envelope** (scope, triggered) | Is the gain right? The burst should peak near 0.5 and never pin at 1.0. Clipping raises the noise floor and reads in the log exactly like a weak signal. |

There is a live **tuner gain** slider, because setting gain against the
envelope view is the whole point. The scope is normal-mode triggered on a
rising edge at 0.05 — without a trigger the display is mostly the 95% of the
time nothing is transmitting, with the burst flashing past unreadably.

This is a **diagnostic tool, not the deployment path.** It needs a desktop
session, VNC or `ssh -X`. `si4463_rt.py` builds the same chain without the Qt
sinks; keep using it to actually run the link.

### How it is put together

The decoder block lives in `si4463_gr_block.py` as ordinary, importable source
— **not** embedded in the `.grc`. GRC normally stores an Embedded Python
Block's whole body as an escaped string inside the flowgraph file, which cannot
be imported, cannot be tested, and produces unreadable diffs; the original
bench flowgraph did that and carries a complete copy of the decoder inside it.
Here the `.grc` embeds only a short subclass that forwards to the module, so
the flowgraph and the headless decoder end in the same block, and a fix to it
fixes both.

### Why `si4463_grc.sh` rather than opening the file directly

GRC evaluates the decoder block's imports when it *loads* the flowgraph, in
order to instantiate the block and discover its ports. If `si4463_gr_block` is
not importable at that moment, GRC cannot find the ports and reports:

```
LookupError: sink key 0 not in sink block keys
>>> Error: Connection between freq_xlating_fir_filter_xxx_0(0)
    and epy_block_0(0) could not be made
```

which is a confusing way to be told the `PYTHONPATH` is wrong. Whether the
import works depends on where `gnuradio-companion` was launched from — a
desktop menu or another terminal, not necessarily this directory. The launcher
sets both the working directory and `PYTHONPATH`, so the flowgraph opens from
anywhere. It also checks for a display up front, rather than letting Qt fail
with "could not connect to display" after a long pause.

## Will the Pi keep up?

This is the question a port to a small machine has to answer, and the packet
log cannot answer it for you: **a decoder that falls behind drops input, and
dropped input looks exactly like a quiet band.** So measure it.

```bash
./si4463_benchmark.py --all
```

It runs the identical GNU Radio flowgraph on synthetic noise as fast as the
machine allows and reports CPU seconds (all threads) per signal second. Under
0.5 is comfortable; above 0.8 there is no margin left for USB interrupts and
CSV flushes. For reference, on an x86 Zen 3 desktop the sweep gives 6–11%
across 250 kS/s to 2.4 MS/s — **a Pi 5 is several times slower than that for
this work**, which is exactly why the benchmark ships rather than a promise.

The live decoder also measures itself as it runs — see
[Reliability](#reliability-what-the-decoder-watches-for).

### Sample rate

The default is **1.2 MS/s**, not the 2.4 MS/s the Windows build used. The
signal is 45 kHz wide and the decoder wants 200 kS/s, so the higher rate only
ever bought a longer filter — tap count scales with `fs / transition`, so
halving the input rate halves the work for a signal nowhere near Nyquist.

If the benchmark calls 1.2 MS/s tight, drop to 250 kS/s. The LO offset must
come down with it: the 60 kHz passband has to stay below Nyquist, so at
250 kS/s the offset can be at most 65 kHz. `si4463_rt.py` refuses an impossible
combination with the arithmetic rather than aliasing quietly.

The RTL2832 only supports **225001–300000 Hz and 900001–3200000 Hz**. Anything
else is silently coerced by the dongle; the decoder compares what it asked for
against what it got and says so.

## RTL-SDR Blog V3 notes

* **`--ppm 0` is genuinely right.** The V3 has a 1 ppm TCXO, which at 433 MHz is
  435 Hz. A generic dongle's 30–60 ppm would be tens of kHz and would *still*
  work — the LO is parked `--lo-offset` above the signal and the passband
  reaches 60 kHz, so large tuner error lands inside it and the decoder removes
  what is left burst by burst.
* **Bias tee is off unless you ask.** `--bias-tee` switches on the V3's 4.5 V
  feed. It is set explicitly either way on every run, because the setting lives
  in the dongle and a previous session can leave it on — into a grounded
  antenna that is a shorted supply.
* **Gain defaults to 0 dB, manual.** Over the ranges this link is used at the
  dongle sees far more signal than it can take: +20 dBm out and ~31 dB of
  free-space loss at 2 m puts roughly −11 dBm on the antenna, and an RTL-SDR
  front end clips near −10 dBm with no gain at all. Raise `--gain` only until
  packets are solid, watching the peak level in the heartbeat: it should sit
  near 0.5, never pinned at 1.0.
* **Don't use `--agc`.** The tuner AGC hunts during the 1.07 s of silence
  between bursts and is still recovering when the next preamble arrives.

* **`--device-args`** passes extra gr-osmosdr rtl options through, e.g.
  `--device-args buffers=32` for more USB buffering on a busy Pi.

## Installing without git

Only needed for a Pi with no network. All three routes end at a
`Decoder_Raspi` directory in your home folder, and all three **lose the
executable bit**, because no Windows filesystem carries one — so the first
command is `bash install.sh`, not `./install.sh`. After that `install.sh`
`chmod +x`es everything and the rest works normally.

Line endings are not a problem: every file in the tree is LF-terminated and a
`.gitattributes` pins it, so there is nothing for `dos2unix` to do. That matters
because a CR after `#!/usr/bin/env bash` means the kernel looks for an
interpreter called `bash\r` and the script does not run at all.

For the two network routes, the Pi needs SSH, which is off by default:

```bash
sudo systemctl enable --now ssh          # on the Pi
hostname -I                              # note the address
```

**`scp` the folder** — Windows 10/11 ship OpenSSH, so this needs nothing
installed:

```powershell
scp -r "C:\Users\np841\OneDrive\Desktop\UASS\Decoder_Raspi" pi@raspberrypi.local:~/
```

**`scp` the tarball** — one file instead of twenty, which removes any chance of
an editor or OneDrive touching something in transit:

```powershell
scp "C:\Users\np841\OneDrive\Desktop\UASS\Decoder_Raspi.tar.gz" pi@raspberrypi.local:~/
```

```bash
cd ~ && tar -xzf Decoder_Raspi.tar.gz    # on the Pi
```

**USB stick** — copy either onto a stick, then on the Pi:

```bash
cp -r /media/$USER/<STICK>/Decoder_Raspi ~/
cd ~/Decoder_Raspi && bash install.sh
```

## What changed from the Windows version

`si4463_core.py` — the actual demodulator, framer and packet parser — is
**unchanged**, so a packet that decodes here decodes identically on the Windows
bench, and `si4463_selftest.py` is the same regression suite it always was.

The chain is the same GNU Radio chain the Windows build ran, now headless:

| Was | Is now | Why |
|-----|--------|-----|
| Qt flowgraph generated by GRC | `si4463_rt.py` — the same blocks in a `gr.top_block`, no Qt | runs over SSH and under systemd with no display |
| decoder embedded in the `.grc` | `si4463_gr_block.py` | importable, testable, shared by the live decoder, benchmark and flowgraph |
| `si4463_rt.bat` | `si4463_rt.sh` | apt installs GNU Radio into the system `python3`, so there is no interpreter to choose |
| `si4463_bench_decode.py` | `si4463_rt.py --wav` | the replay path is the live path, so a separate batch decoder was duplicate code |
| — | stall, sample-loss and decoder-error watchdogs | each of those otherwise looks like a quiet band |

The original bench flowgraph is kept in `reference/` for provenance. It
documents the physical layer at length. Nothing imports it.

## Layout

```
si4463_core.py              the decoder. Unchanged from the Windows tree.
si4463_csv.py               one CSV row per packet, flushed as decoded.

si4463_gr_block.py          GNU Radio sink block around the decoder, and the
                            filter constants every chain shares.
si4463_rt.py                the live decoder: headless GNU Radio flowgraph,
                            osmosdr -> freq_xlating_fir_filter -> sink, with
                            the reliability watchdogs.
si4463_benchmark.py         does this Pi keep up? Same flowgraph, timed.

si4463_rpi.grc              diagnostic flowgraph: spectrum, waterfall, burst
                            envelope, live gain. Validated with grcc.
si4463_grc.sh               opens the flowgraph with CWD and PYTHONPATH set,
                            so GRC can resolve the decoder block.

si4463_selftest.py          decoder regression tests (unchanged)
si4463_check_capture.py     "is the transmitter even in this recording?"
si4463_tnc.py               soundcard front end; see below.

install.sh                  Bookworm setup, idempotent
si4463_rt.sh                launcher that runs from the script directory
si4463-decoder.service.in   systemd template; install.sh fills it in

test_current_layout.wav     reference capture, 3.9 s, six known packets
reference/                  the original GNU Radio flowgraph, for provenance
logs/                       packet CSVs (not tracked)
```

## The soundcard TNC

`si4463_tnc.py` is carried over and works — `sounddevice` is PortAudio and
PortAudio is ALSA here — but it is the less useful path on a Pi, where the SDR
is attached directly and there is no reason to route audio through a demodulator
first. Its docstring still describes the Windows setup it was written for
(SDR++ into VB-CABLE); on Linux the equivalent is any NFM demodulator feeding a
loopback device, e.g. `snd-aloop`. The audio must be a **raw discriminator**
output: 2GFSK spends long runs on one rail so its information sits at and near
DC, and de-emphasis, a DC blocker, audio AGC or a voice filter all eat exactly
that. Its references to freezing a `.exe` do not apply.

```bash
sudo apt install python3-sounddevice libportaudio2
python3 si4463_tnc.py --list-devices
```

## License

GPL-3.0, following the GNU Radio flowgraphs the decoder grew out of.
