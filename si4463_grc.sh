#!/usr/bin/env bash
#
# SPDX-License-Identifier: GPL-3.0
#
# Open the diagnostic flowgraph in GNU Radio Companion.
#
# This exists because of one specific failure. si4463_rpi.grc's decoder block
# imports si4463_gr_block, which imports si4463_core - and GRC evaluates that
# import when it loads the flowgraph, in order to instantiate the block and
# discover how many ports it has. If the import fails, GRC cannot determine the
# ports, the connection into the decoder cannot be made, and the flowgraph is
# reported as invalid:
#
#     LookupError: sink key 0 not in sink block keys
#     >>> Error: Connection between freq_xlating_fir_filter_xxx_0(0) and
#         epy_block_0(0) could not be made
#
# Which is a confusing way to be told "your PYTHONPATH is wrong". The import
# only succeeds if this directory is importable, and GRC's working directory is
# wherever gnuradio-companion was launched from - the desktop menu, a file
# manager, or a terminal in some other folder. Verified both ways: from this
# directory it loads, from elsewhere it fails with the above, and with
# PYTHONPATH set it loads from anywhere.
#
# So set both, and the flowgraph opens from anywhere:
#
#     ./si4463_grc.sh                 # open si4463_rpi.grc
#     ./si4463_grc.sh --help          # gnuradio-companion's own options
#
# The headless decoder needs none of this: si4463_rt.py builds the same chain
# in Python, without Qt sinks, and runs over SSH with no display.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$HERE"
export PYTHONPATH="${HERE}${PYTHONPATH:+:${PYTHONPATH}}"

if ! command -v gnuradio-companion >/dev/null 2>&1; then
    cat >&2 <<'EOF'
gnuradio-companion is not installed.

    sudo apt install gnuradio gr-osmosdr

./install.sh installs it, along with everything the headless decoder needs.

If gr-osmosdr is unavailable on your release, GNU Radio 3.10 also ships
gr-soapy in-tree; install soapysdr-module-rtlsdr and swap the osmosdr source
block for a Soapy source with driver=rtlsdr.
EOF
    exit 1
fi

# A flowgraph with Qt sinks needs somewhere to draw. Caught here rather than
# left to Qt's own "could not connect to display" further down, because over a
# plain SSH session that message arrives after a long pause and reads like a
# GNU Radio fault rather than a missing display.
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    cat >&2 <<'EOF'
No display found (neither DISPLAY nor WAYLAND_DISPLAY is set).

This flowgraph draws a spectrum, a waterfall and a scope, so it needs a
desktop session on the Pi, VNC, or ssh -X. Options:

  * run it on the Pi's own desktop, or over VNC
  * ssh -X pi@raspberrypi.local   then re-run this script
  * to check the GNU Radio chain itself with no display and no dongle:
        ./si4463_rt.py --wav test_current_layout.wav
  * to actually run the link headless, with no display:
        ./si4463_rt.py
EOF
    exit 1
fi

exec gnuradio-companion "${@:-si4463_rpi.grc}"
