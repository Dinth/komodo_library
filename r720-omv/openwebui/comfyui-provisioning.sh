#!/bin/bash
#
# Purpose:      ai-dock ComfyUI provisioning. On first boot, download the
#               DreamShaper XL Turbo checkpoint (versatile/style-rich, ~8-step
#               Turbo model) into persistent /workspace storage. ai-dock's
#               init.sh sources this file, then symlinks storage into ComfyUI's
#               models/checkpoints directory.
# Dependencies: wget (shipped in the ai-dock base image); WORKSPACE env var set
#               by the container (defaults to /workspace).
# Author:       AI
#
# Referenced by the comfyui service via PROVISIONING_SCRIPT (raw GitHub URL).
# This file is *sourced* into init.sh, so `set -euo pipefail` is confined to a
# subshell — a transient download failure must not abort the container's init.

# provisioning_start: ai-dock's init invokes this (and the bottom call also runs
# it when sourced standalone). Idempotent: wget -nc skips an already-present file.
function provisioning_start() {
    (
        set -euo pipefail
        ckpt_dir="${WORKSPACE:-/workspace}/storage/stable_diffusion/models/ckpt"
        mkdir -p "$ckpt_dir"
        wget -nc --content-disposition -P "$ckpt_dir" \
            "https://huggingface.co/Lykon/dreamshaper-xl-v2-turbo/resolve/main/DreamShaperXL_Turbo_v2.safetensors"
    )
}

provisioning_start
