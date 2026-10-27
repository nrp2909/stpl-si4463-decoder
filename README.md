# Si4463 Packet Decoder — Raspberry Pi

The UASS telemetry decoder, ported to run on a Raspberry Pi 5 (Debian Bookworm)
with an RTL-SDR Blog V3. It takes the signal as complex baseband straight off
the dongle and prints and logs each `SensorPacket_t` as it arrives.

This is the `Decoder` tree with its GNU Radio dependency removed. The decoder
itself is byte-identical to the Windows one.

## What changed, and what did not

`si4463_core.py` — the actual demodulator, framer and packet parser — is
**unchanged**. It was always numpy-only and knew nothing about GNU Radio, so a
packet that decodes here decodes identically on the Windows bench, and
`si4463_selftest.py` is the same regression suite it always was.

Only the front end was rewritten, because only the front end was GNU Radio:

| Was | Is now | Why |
|-----|--------|-----|
| `osmosdr.source` | `si4463_rtlsdr.py` — ctypes onto `librtlsdr` | gr-osmosdr is a large dependency for one block, and the piece most often missing from a Pi image |
| `filter.freq_xlating_fir_filter_ccf` | `si4463_dsp.py` — numpy | ~100 lines, and it drops the last non-apt dependency |
| `firdes.low_pass` | `si4463_dsp.lowpass_taps` | same Hamming-windowed sinc, same tap-count heuristic |
| `si4463_rt.bat` | `si4463_rt.sh` | no interpreter to choose; system `python3` has apt numpy |
| `si4463_bench_decode.py` | `si4463_rt.py --wav` | the replay path is the live path, so a separate batch decoder was duplicate code |

The result depends on **numpy and nothing else**. No pip, no virtualenv — which
also sidesteps Bookworm's PEP 668 "externally-managed-environment" refusal,
since `python3-numpy` comes from apt and is the faster build on ARM anyway.

The original GNU Radio flowgraph is kept in `reference/` for provenance. It
documents the physical layer at length and is what the numpy chain was written
against. Nothing imports it and it is not needed to run anything.

## Getting it onto the Pi

Four ways; **git is the one to use** if the Pi has network, because it is also
how you update later. The others are here for an offline Pi.

### A. `git clone` — recommended

```bash
git clone https://github.com/nrp2909/stpl-si4463-decoder.git
cd stpl-si4463-decoder
./install.sh
```

Nothing else is needed: `git` is already on Raspberry Pi OS, the repo is
public so no credentials are involved, and the clone is about 3 MB.

The executable bits are recorded **in the repo**, so the scripts are runnable
straight out of the clone — this is the one transfer method where `./install.sh`
works without `bash` in front of it.

To pick up later changes:

```bash
git pull
```

`logs/` and the generated `si4463-decoder.service` are in `.gitignore`, so a
`git pull` never fights your packet logs or your local unit file.

---

The remaining three end at the same place — a `Decoder_Raspi` directory in your
home folder — and all of them **lose the executable bit**, because no Windows
filesystem carries one. Use `bash install.sh` for the first command; it
`chmod +x`es everything else.

For the two network options the Pi needs SSH, which is off by default on
Raspberry Pi OS:

```bash
sudo systemctl enable --now ssh          # on the Pi
hostname -I                              # note the address
```

### B. `scp` from Windows — one command

Windows 10/11 ship OpenSSH, so this works from PowerShell or Git Bash with
nothing installed:

```powershell
scp -r "C:\Users\np841\OneDrive\Desktop\UASS\Decoder_Raspi" pi@raspberrypi.local:~/
```

Replace `pi@raspberrypi.local` with your account and the address from
`hostname -I` if the `.local` name does not resolve.

### C. The tarball — one file

`Decoder_Raspi.tar.gz` sits next to the folder, beside the existing
`Decoder.zip`. Moving one file instead of nineteen avoids any chance of an
editor or OneDrive touching a file in transit:

```powershell
scp "C:\Users\np841\OneDrive\Desktop\UASS\Decoder_Raspi.tar.gz" pi@raspberrypi.local:~/
```

```bash
cd ~ && tar -xzf Decoder_Raspi.tar.gz    # on the Pi
```

### D. USB stick — no network

Copy either the folder or the tarball onto a stick, then on the Pi:

```bash
cp -r /media/$USER/<STICK>/Decoder_Raspi ~/
```

### What a file copy does and does not break

Every file in this tree is already **LF**-terminated, checked before packaging,
so there is nothing for `dos2unix` to do — a CR after `#!/usr/bin/env bash`
would stop the scripts exec'ing at all, which is why it was checked rather than
assumed.

What *is* lost is the **executable bit**: Windows filesystems do not carry one,
and neither does FAT32 on a stick. That is why the first command below is
`bash install.sh` and not `./install.sh` — and `install.sh` then `chmod +x`es
everything else, so `./si4463_rt.py` works from that point on.

## Install

On the Pi, in whichever directory you ended up with:

```bash
cd ~/stpl-si4463-decoder    # from a git clone
./install.sh

cd ~/Decoder_Raspi          # from a file copy (B, C or D)
bash install.sh             # 'bash', because the exec bit was lost
```

It is idempotent, so re-run it any time. It installs `python3-numpy`,
`rtl-sdr` and `librtlsdr0`; blacklists the DVB-T kernel driver; checks udev
permissions; generates the systemd unit for your account and path; runs both
test suites; decodes the bundled reference capture; and finishes by
benchmarking your Pi to recommend a sample rate.

**The DVB-T driver is the thing that bites.** The dongle announces itself as a
TV receiver, the kernel binds `dvb_usb_rtl28xxu` to it on plug-in, and
librtlsdr then cannot claim the USB interface — which surfaces as "no SDR
found" or `usb_claim_interface error -6`. `install.sh` blacklists it and
unloads it, but **replug the dongle afterwards**. If you were added to
`plugdev`, log out and back in too.

## Run

```bash
./si4463_rt.py                                  # live, 435.000 MHz
./si4463_rt.py --rate 250e3 --lo-offset 50e3    # low-CPU mode
./si4463_rt.py --list-devices
./si4463_rt.py --wav test_current_layout.wav    # replay, no radio needed
```

Headless:

```bash
sudo cp si4463-decoder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now si4463-decoder
journalctl -u si4463-decoder -f
```

## Will the Pi keep up?

This is the question a port to a small machine has to answer, and the packet
log cannot answer it for you: **a decoder that falls behind drops input, and
dropped input looks exactly like a quiet band.** So measure it.

```bash
./si4463_benchmark.py --all
```

It pushes synthetic samples through the identical chain and reports processing
seconds per signal second. Under 0.5 is comfortable; above 0.8 there is no
margin left for USB interrupts and CSV flushes. For reference, on an x86 Zen 3
desktop the sweep gives 7% at 250 kS/s and 15% at 1.2 MS/s — **a Pi 5 is
several times slower than that for this work**, which is exactly why the
benchmark ships rather than a promise.

The live decoder also measures itself as it runs: `--status` prints the load
and any dropped buffers periodically, it says so once if load passes 80%, and
the final summary reports drops explicitly rather than letting them pass as
silence.

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

### The `--pipe` fallback and its gain trap

If `librtlsdr` cannot be loaded, `--pipe` runs the `rtl_sdr` command and reads
its stdout. It is equivalent in every respect but one: `rtl_sdr` parses `-g`
into tenths of a dB and then treats **zero as "enable AGC"** —

```c
if (0 == gain) { verbose_auto_gain(dev); } else { ... }
```

— so `rtl_sdr -g 0` does not mean 0 dB, it means the control loop this link
must not use. The pipe source substitutes 0.9 dB, the R820T2's next step up,
and prints that it did. Through librtlsdr the distinction is expressible and
0 dB manual is really 0 dB, which is why that is the default path.

## Layout

```
si4463_core.py              the decoder. Unchanged from the Windows tree.
si4463_csv.py               one CSV row per packet, flushed as decoded.

si4463_dsp.py               NEW. Frequency-translating decimating FIR in numpy,
                            replacing GNU Radio's block and firdes.
si4463_rtlsdr.py            NEW. RTL-SDR via ctypes->librtlsdr (async), with an
                            rtl_sdr pipe fallback and a WAV replay source.
si4463_rt.py                NEW. The live front end tying those together.
si4463_benchmark.py         NEW. Does this Pi keep up?

si4463_selftest.py          decoder regression tests (unchanged)
si4463_dsp_selftest.py      NEW. Tests for the numpy front end.
si4463_check_capture.py     "is the transmitter even in this recording?"
si4463_tnc.py               soundcard front end; see below.

install.sh                  Bookworm setup, idempotent
si4463_rt.sh                launcher that runs from the script directory
si4463-decoder.service.in   systemd template; install.sh fills it in

test_current_layout.wav     reference capture, 3.9 s, six known packets
reference/                  the original GNU Radio flowgraph, for provenance
logs/                       packet CSVs (not tracked)
```

## Testing without a transmitter

Three checks, in increasing strength, all runnable on the Pi with no radio:

```bash
./si4463_dsp_selftest.py     # the numpy front end against a slow reference
./si4463_selftest.py         # the decoder, synthesised frames buried in noise
./si4463_rt.py --wav test_current_layout.wav
```

The third is the real one: a genuine 3.9 s recording containing six known
packets, through the identical chain the live path uses. It should report
**6 packets from 6 bursts, 0 with bit errors, 0 lost by counter**, with
counters 0–5 and ~18.3 dB SNR.

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
python3 si4463_tnc.py --list-devices
```

## Logging

Every run writes `logs/packets_<date>_<time>.csv`, one row per packet, flushed
immediately, so the file is a live record rather than something that only
exists once the program is stopped. `--csv <path>` moves it, `--no-csv` turns it
off. The `logs/` directory is not tracked.

## License

GPL-3.0, following the GNU Radio flowgraphs the decoder grew out of.
