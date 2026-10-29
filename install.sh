#!/usr/bin/env bash
#
# SPDX-License-Identifier: GPL-3.0
#
# Set up the Si4463 decoder on Raspberry Pi OS / Debian Bookworm.
#
# The decoder is a GNU Radio flowgraph: gr-osmosdr reads the dongle and GNU
# Radio's own frequency-translating FIR does the filtering. Everything comes
# from apt. There is deliberately no pip and no virtualenv: Bookworm's Python
# is marked externally-managed (PEP 668), and apt's gnuradio is built against
# the system VOLK, which is what makes the filter fast on ARM.
#
# The gnuradio package is roughly 1.5 GB with its dependencies. That is the
# price of the whole chain being GNU Radio, and it buys the diagnostic
# flowgraph (si4463_rpi.grc) as well as the headless decoder.
#
# Safe to re-run; every step checks before it acts.

set -euo pipefail

BLACKLIST=/etc/modprobe.d/blacklist-rtl-sdr-decoder.conf
UDEV_FALLBACK=/etc/udev/rules.d/99-rtl-sdr-decoder.rules
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m  %s\n' "$*"; }
bad()  { printf '   \033[31mFAIL\033[0m  %s\n' "$*"; }

if [[ ${EUID} -eq 0 ]]; then
    echo "Run this as your normal user, not root -- it needs to know which"
    echo "account to add to the plugdev group, and \$USER would be 'root'."
    echo "It calls sudo itself where it needs to."
    exit 1
fi

say "System"
if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    ok "${PRETTY_NAME:-unknown}"
    if [[ "${VERSION_CODENAME:-}" != "bookworm" ]]; then
        warn "written for Bookworm; ${VERSION_CODENAME:-this release} may differ"
    fi
fi
if [[ -r /proc/device-tree/model ]]; then
    ok "$(tr -d '\0' < /proc/device-tree/model)"
fi
ok "kernel $(uname -r) on $(uname -m)"

say "Packages"
# gnuradio         the runtime: scheduler, filter blocks, firdes, VOLK
# gr-osmosdr       osmosdr.source, which drives the RTL-SDR through librtlsdr
# rtl-sdr          rtl_test / rtl_biast, and the udev rules
NEED=(gnuradio gr-osmosdr rtl-sdr)
# Optional, for the soundcard TNC path only. Not required by si4463_rt.py.
OPTIONAL=(python3-sounddevice libportaudio2)

MISSING=()
for pkg in "${NEED[@]}" "${OPTIONAL[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        MISSING+=("$pkg")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "   installing: ${MISSING[*]}"
    sudo apt-get update
    # The optional ones are installed in the same transaction but their failure
    # is not fatal: a headless Pi with no audio stack should still get a
    # working SDR decoder.
    sudo apt-get install -y "${NEED[@]}" || {
        bad "could not install the required packages"; exit 1; }
    sudo apt-get install -y "${OPTIONAL[@]}" 2>/dev/null \
        || warn "optional audio packages unavailable; the soundcard TNC will not run"
else
    ok "all packages already present"
fi

for pkg in "${NEED[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 && ok "$pkg $(dpkg-query -W -f='${Version}' "$pkg")"
done

say "DVB-T kernel driver"
# This is the single most common reason an RTL-SDR "is not found" on Linux. The
# dongle advertises itself as a DVB-T receiver, the kernel binds
# dvb_usb_rtl28xxu to it on plug-in, and librtlsdr then cannot claim the USB
# interface. Blacklisting the module is the fix; unloading it makes the fix
# take effect without a reboot.
if [[ -f ${BLACKLIST} ]]; then
    ok "already blacklisted (${BLACKLIST})"
else
    sudo tee "${BLACKLIST}" >/dev/null <<'EOF'
# Keep the kernel's DVB-T driver off RTL-SDR dongles so librtlsdr can claim
# them. Written by the Si4463 decoder's install.sh.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
blacklist dvb_usb_rtl2832u
EOF
    ok "wrote ${BLACKLIST}"
fi

if lsmod | grep -q '^dvb_usb_rtl28xxu'; then
    warn "dvb_usb_rtl28xxu is loaded right now; unloading it"
    sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null \
        || warn "could not unload it -- unplug the dongle and replug, or reboot"
fi
if lsmod | grep -q '^dvb_usb_rtl28xxu'; then
    bad "still loaded. Replug the dongle (or reboot) before running the decoder."
else
    ok "not loaded"
fi

say "Device permissions"
# Debian's librtlsdr0 ships udev rules that open the device up to users. Only
# if those are missing is a fallback rule written, so the distribution's own
# file stays authoritative where it exists.
if ls /lib/udev/rules.d/*rtlsdr* /usr/lib/udev/rules.d/*rtlsdr* \
      /etc/udev/rules.d/*rtlsdr* >/dev/null 2>&1; then
    ok "udev rules supplied by the distribution"
elif [[ -f ${UDEV_FALLBACK} ]]; then
    ok "fallback udev rule already in place"
else
    # 0bda:2838 is the RTL2838UHIDIR, which is what the RTL-SDR Blog V3 reports.
    sudo tee "${UDEV_FALLBACK}" >/dev/null <<'EOF'
# Fallback: let the plugdev group open RTL-SDR dongles. Written by the Si4463
# decoder's install.sh, only because no distribution rule was found.
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", MODE="0660", GROUP="plugdev"
EOF
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    ok "wrote ${UDEV_FALLBACK} and reloaded udev"
fi

if id -nG "$USER" | tr ' ' '\n' | grep -qx plugdev; then
    ok "$USER is in plugdev"
else
    sudo usermod -aG plugdev "$USER"
    warn "added $USER to plugdev -- log out and back in for it to take effect"
fi

say "Dongle"
if lsusb 2>/dev/null | grep -qi 'RTL2838\|RTL2832\|Realtek.*DVB'; then
    ok "$(lsusb | grep -i 'RTL2838\|RTL2832\|Realtek.*DVB' | head -1)"
    # rtl_test -t queries the tuner and exits; it is the cheapest end-to-end
    # proof that the library can claim the device and talk to the R820T2.
    if timeout 20 rtl_test -t 2>&1 | tee /tmp/rtl_test.$$ | grep -q 'Found.*device'; then
        ok "rtl_test opened it: $(grep -m1 'tuner' /tmp/rtl_test.$$ || true)"
    else
        bad "rtl_test could not open it. Output:"
        sed 's/^/        /' /tmp/rtl_test.$$ || true
        warn "if it says 'usb_claim_interface error -6', the DVB driver still"
        warn "has it: replug the dongle, or reboot, and re-run this script."
    fi
    rm -f /tmp/rtl_test.$$
else
    warn "no RTL-SDR seen on USB. Plug it in and re-run to verify the setup."
fi

say "systemd unit"
# Filled in rather than copied: the template carries placeholders because
# Bookworm has no default "pi" account and the install path is wherever this
# tree was unpacked.
if [[ -f si4463-decoder.service.in ]]; then
    sed -e "s|__USER__|${USER}|g" -e "s|__DIR__|${HERE}|g" \
        "${HERE}/si4463-decoder.service.in" > "${HERE}/si4463-decoder.service"
    ok "generated si4463-decoder.service for ${USER} in ${HERE}"
else
    warn "si4463-decoder.service.in missing; skipping"
fi

say "Decoder"
cd "${HERE}"
chmod +x si4463_rt.py si4463_benchmark.py si4463_tnc.py \
         si4463_selftest.py si4463_check_capture.py \
         si4463_gr_block.py si4463_rt.sh si4463_grc.sh 2>/dev/null || true
ok "scripts made executable"

if python3 -c 'from gnuradio import gr; import osmosdr' >/dev/null 2>&1; then
    ok "GNU Radio $(python3 -c 'from gnuradio import gr; print(gr.version())' 2>/dev/null) with gr-osmosdr"
else
    bad "python3 cannot import gnuradio and osmosdr:"
    python3 -c 'from gnuradio import gr; import osmosdr' 2>&1 | tail -3 | sed 's/^/        /'
    exit 1
fi

# The test suite and the reference capture are the evidence that it works on
# this machine before any antenna is involved.

if python3 si4463_selftest.py >/tmp/core.$$ 2>&1; then
    ok "si4463_selftest.py: $(tail -1 /tmp/core.$$)"
else
    bad "si4463_selftest.py failed:"; sed 's/^/        /' /tmp/core.$$
fi
rm -f /tmp/core.$$

# Decode the reference capture. This is the strongest check available without
# a transmitter: a real 3.9 s recording containing six known packets, through
# the identical GNU Radio chain the live path uses, with a wavfile_source in
# place of the osmosdr source.
if python3 si4463_rt.py --wav test_current_layout.wav --no-csv --heartbeat 0 \
        >/tmp/wav.$$ 2>&1; then
    ok "reference capture: $(grep -m1 'Stopped after' /tmp/wav.$$ || echo 'see /tmp')"
else
    bad "reference capture failed:"; sed 's/^/        /' /tmp/wav.$$
fi
rm -f /tmp/wav.$$

say "Diagnostic flowgraph"
# Proves the flowgraph loads and that every block and parameter resolves on
# this machine. PYTHONPATH is set for the reason si4463_grc.sh explains at
# length.
if command -v grcc >/dev/null 2>&1; then
    if PYTHONPATH="${HERE}" grcc -o /tmp "${HERE}/si4463_rpi.grc" >/tmp/grcc.$$ 2>&1; then
        ok "si4463_rpi.grc compiles (grcc) -- ./si4463_grc.sh will open it"
    else
        warn "si4463_rpi.grc did not compile:"
        sed 's/^/        /' /tmp/grcc.$$ | head -8
    fi
    rm -f /tmp/grcc.$$ /tmp/si4463_rpi.py /tmp/si4463_rpi_epy_block_0.py
else
    warn "grcc not found; the flowgraph was not checked"
fi

say "Choosing a sample rate for this Pi"
# The port's real question: whether this machine keeps up, which only this
# machine can answer. The filter's tap
# count scales with the input rate, so the lowest workable rate is the cheapest
# and the recommendation is simply the lowest rate with comfortable margin.
python3 si4463_benchmark.py --all -s 6 2>&1 | sed 's/^/   /'

cat <<EOF

$(printf '\033[1m== Next\033[0m')

   Live decode (settings default to 433.400 MHz, 1.2 MS/s):

       ./si4463_rt.py

   If the benchmark above called 1.200 MS/s "tight" or worse, use the
   lowest rate it called comfortable -- at 250 kS/s the LO offset has to
   come down too, because the 60 kHz passband must stay below Nyquist:

       ./si4463_rt.py --rate 250e3 --lo-offset 50e3

   Look at the signal (needs a desktop, VNC or ssh -X):

       ./si4463_grc.sh

   Check what it sees without a transmitter:

       ./si4463_rt.py --list-devices
       ./si4463_rt.py --wav test_current_layout.wav

   Run headless at boot:

       sudo cp si4463-decoder.service /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now si4463-decoder
       journalctl -u si4463-decoder -f

   Packets land in logs/packets_<date>_<time>.csv, one row each, flushed
   as they arrive.
EOF

if ! id -nG "$USER" | tr ' ' '\n' | grep -qx plugdev; then
    printf '\n   \033[33mLog out and back in first\033[0m -- the plugdev group is not active in this shell.\n'
fi
