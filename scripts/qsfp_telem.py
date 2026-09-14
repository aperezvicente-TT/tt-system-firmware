#!/usr/bin/env python3
# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""Compatibility wrapper for the original QSFP discovery telemetry tool."""

from qsfp import telem_main


if __name__ == "__main__":
    telem_main()
