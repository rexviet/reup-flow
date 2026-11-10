#!/bin/sh
# Local fixture only. No provider, credentials, or production storage needed.
set -eu
cd "$(dirname "$0")"
test "$#" -eq 2 || { echo 'usage: sh prepare-media.sh SOURCE_VIDEO MIXED_AUDIO' >&2; exit 2; }
ffmpeg -v error -nostdin -y -ss 24 -i "$1" -t 8 -an -c:v libx264 -pix_fmt yuv420p project/source.mp4
ffmpeg -v error -nostdin -y -ss 24 -i "$2" -t 8 project/mixed.wav
