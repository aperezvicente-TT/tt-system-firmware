/*
 * Copyright (c) 2026 Tenstorrent AI ULC
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef APP_DMC_QSFP_H_
#define APP_DMC_QSFP_H_

#include <stdint.h>

/*
 * Probe the four P150a QSFP-DD cages on i2c1: report per-cage module presence
 * and, for any seated module, read and log the CMIS Identifier byte. Read-only
 * bring-up helper; see qsfp.c for the wiring/protocol details.
 *
 * Returns a packed status word, one byte per cage (byte 0 = cage A ... byte 3 =
 * cage D). Per-cage byte:
 *   0x00        expander absent (no I2C response)
 *   0x01        expander present, no module seated
 *   0x02        module seated, CMIS 0x50 read failed
 *   0x80 | id   module present; id = CMIS SFF-8024 identifier byte
 * This is forwarded to the SMC as telemetry TAG_QSFP_STATUS so cage state is
 * readable from the host over PCIe (no JTAG probe required).
 */
uint32_t qsfp_discover(void);

#endif /* APP_DMC_QSFP_H_ */
