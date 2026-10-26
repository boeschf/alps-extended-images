#!/usr/bin/env bash
# Each component installs and removes its build-only APT packages in one layer.
# Python build tools and installed component wheels remain available to later steps.
# shellcheck source=Alps-Images/common/package-helpers.sh
source /opt/alps/package-helpers.sh

vllm_build_begin() {
    export PIP_CONSTRAINT=/opt/alps/vllm-base-pins.txt
    vllm_apt_build_deps=(
        build-essential cargo cmake curl git libdw-dev libnuma-dev
        ninja-build pkg-config rustc python3-dev
    )
    vllm_apt_install_packages=(
        ca-certificates libibverbs1 libibverbs-dev libnl-3-dev libnl-route-3-dev
        "${vllm_apt_build_deps[@]}"
    )
    snapshot_apt_packages /tmp/vllm-apt-before.txt
    apt_get update
    apt_get install -y --no-install-recommends "${vllm_apt_install_packages[@]}"
    rm -rf /var/lib/apt/lists/*
}

vllm_build_end() {
    APT_CLEANUP_HOLD_PACKAGES="libibverbs1 libibverbs-dev" \
        cleanup_new_apt_build_deps /tmp/vllm-apt-before.txt "${vllm_apt_build_deps[@]}"
    rm -f /tmp/vllm-apt-before.txt
    rm -rf /root/.cache/pip
}
