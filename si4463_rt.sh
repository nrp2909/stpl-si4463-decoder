#!/usr/bin/env bash
#
# SPDX-License-Identifier: GPL-3.0
#
# Launcher for the live decoder, replacing si4463_rt.bat.
#
# On Windows the batch file existed to spell out the radioconda interpreter,
# because gnuradio and osmosdr lived only there. Here there is nothing to
# choose: apt installs GNU Radio and gr-osmosdr into the system python3. So
# this script does the one thing still worth doing, which is to
# run from the directory the scripts live in - the CSV log is written to
# ./logs/ relative to the script, and launching from elsewhere (a cron entry, a
# desktop shortcut, another terminal) would otherwise scatter logs around the
# filesystem or fail on a read-only working directory.
#
# All arguments are passed through:
#
#     ./si4463_rt.sh
#     ./si4463_rt.sh --rate 250e3 --lo-offset 50e3
#     ./si4463_rt.sh --wav test_current_layout.wav

set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
exec python3 ./si4463_rt.py "$@"
