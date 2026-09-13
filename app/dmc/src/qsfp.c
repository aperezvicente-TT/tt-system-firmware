/*
 * Copyright (c) 2026 Tenstorrent AI ULC
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * QSFP-DD cage discovery (bring-up milestone M1).
 *
 * P150a routes all four QSFP-DD cages onto the DMC's i2c1 controller
 * (schematic net "MCU_I2C0", sheets 3 + 28-31):
 *   - one TCA9554 (PCA9554-compatible) GPIO expander per cage carries the
 *     module sideband, address-strapped A=0x38 B=0x39 C=0x3a D=0x3b;
 *   - the CMIS module management EEPROM (I2C addr 0x50) is a *shared* bus
 *     across all four cages - the cage whose ModSelL is asserted (driven low)
 *     is the one that answers at 0x50. There is no I2C mux.
 *
 * TCA9554 bit map (identical for all four cages, from the schematic pin
 * numbers P0=pin4 P1=pin5 P2=pin6 P3=pin7 P4=pin9):
 *   P0 = MODPRSL (input,  active low: low  => module seated)
 *   P1 = RESETL  (output, active low)
 *   P2 = MODSELL (output, active low: low  => this cage owns the 0x50 bus)
 *   P3 = LPMODE  (output, active high)
 *   P4 = INTL    (input,  active low, open-drain)
 *   P5..P7 unused
 *
 * This routine only *reads* identity. It drives each expander to a safe idle
 * state (module out of reset, deselected, low-power), checks presence, then
 * for each seated module asserts ModSelL and reads the CMIS Identifier byte.
 * It does not power the optics up or select an application (that is M3).
 */

#include "qsfp.h"

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

LOG_MODULE_REGISTER(qsfp, CONFIG_TT_APP_LOG_LEVEL);

/* TCA9554 / PCA9554 command registers. */
#define TCA9554_REG_INPUT  0x00
#define TCA9554_REG_OUTPUT 0x01
#define TCA9554_REG_CONFIG 0x03 /* 1 = input, 0 = output; POR default 0xFF */

/* Expander port-bit assignments (see file header). */
#define QSFP_BIT_MODPRSL BIT(0)
#define QSFP_BIT_RESETL  BIT(1)
#define QSFP_BIT_MODSELL BIT(2)
#define QSFP_BIT_LPMODE  BIT(3)
#define QSFP_BIT_INTL    BIT(4)

/*
 * Direction: MODPRSL and INTL are inputs; the unused P5..P7 are left as inputs
 * too. RESETL/MODSELL/LPMODE are outputs.
 */
#define QSFP_CONFIG_DIR (QSFP_BIT_MODPRSL | QSFP_BIT_INTL | BIT(5) | BIT(6) | BIT(7))

/*
 * Idle output level: RESETL=1 (out of reset), MODSELL=1 (deselected),
 * LPMODE=1 (low power). Input bits are don't-care so just drive the whole byte
 * high.
 */
#define QSFP_OUT_IDLE   0xFF
/* Idle, but with this cage selected onto the shared 0x50 bus (MODSELL low). */
#define QSFP_OUT_SELECT (QSFP_OUT_IDLE & ~QSFP_BIT_MODSELL)

/* Shared CMIS module management address and the two identity registers. */
#define QSFP_MODULE_I2C_ADDR 0x50
#define CMIS_REG_IDENTIFIER  0x00 /* SFF-8024 identifier */
#define CMIS_REG_REVISION    0x01 /* CMIS revision (nibble.nibble) */

/*
 * Per-cage status byte for the packed word returned by qsfp_discover() and
 * forwarded to the SMC as telemetry TAG_QSFP_STATUS. See qsfp.h.
 */
#define QSFP_ST_EXPANDER_ABSENT 0x00
#define QSFP_ST_NO_MODULE       0x01
#define QSFP_ST_READ_FAILED     0x02
#define QSFP_ST_PRESENT_FLAG    0x80 /* OR'd with the CMIS identifier byte */

struct qsfp_cage {
	const char *name;
	uint8_t expander_addr;
};

static const struct qsfp_cage cages[] = {
	{"A", 0x38},
	{"B", 0x39},
	{"C", 0x3a},
	{"D", 0x3b},
};

/* Decode the SFF-8024 module identifier byte. */
static const char *cmis_identifier_str(uint8_t id)
{
	switch (id) {
	case 0x0c:
		return "QSFP";
	case 0x0d:
		return "QSFP+";
	case 0x11:
		return "QSFP28";
	case 0x18:
		return "QSFP-DD (CMIS)";
	case 0x19:
		return "OSFP (CMIS)";
	case 0x1e:
		return "QSFP+ w/ CMIS";
	case 0x00:
	case 0xff:
		return "unspecified";
	default:
		return "unknown";
	}
}

uint32_t qsfp_discover(void)
{
	const struct device *bus = DEVICE_DT_GET(DT_NODELABEL(i2c1));
	uint32_t status = 0;

	if (!device_is_ready(bus)) {
		LOG_ERR("QSFP: i2c1 bus not ready, skipping discovery");
		return status;
	}

	LOG_INF("QSFP-DD discovery: probing %d cages on i2c1", (int)ARRAY_SIZE(cages));

	/*
	 * Put every cage's expander into the safe idle state first, so that no
	 * cage is holding ModSelL low while we probe another - only one cage may
	 * own the shared 0x50 bus at a time. Write the output level before the
	 * direction so the pins never glitch through an unintended state.
	 */
	ARRAY_FOR_EACH(cages, i) {
		const struct qsfp_cage *c = &cages[i];

		if (i2c_reg_write_byte(bus, c->expander_addr, TCA9554_REG_OUTPUT,
				       QSFP_OUT_IDLE) != 0) {
			continue; /* absence is reported in the probe loop below */
		}
		(void)i2c_reg_write_byte(bus, c->expander_addr, TCA9554_REG_CONFIG,
					 QSFP_CONFIG_DIR);
	}

	ARRAY_FOR_EACH(cages, i) {
		const struct qsfp_cage *c = &cages[i];
		uint8_t input;
		uint8_t id = 0;
		uint8_t rev = 0;
		int id_ret;
		int rev_ret;

		if (i2c_reg_read_byte(bus, c->expander_addr, TCA9554_REG_INPUT, &input) != 0) {
			LOG_WRN("QSFP %s: expander 0x%02x not responding - cage unpopulated?",
				c->name, c->expander_addr);
			continue;
		}

		/* MODPRSL is active low: bit clear => a module is seated. */
		if ((input & QSFP_BIT_MODPRSL) != 0) {
			LOG_INF("QSFP %s: expander ok (0x%02x), no module seated", c->name,
				c->expander_addr);
			status |= (uint32_t)QSFP_ST_NO_MODULE << (8 * i);
			continue;
		}

		/* Select this cage (and only this cage) onto the shared 0x50 bus. */
		if (i2c_reg_write_byte(bus, c->expander_addr, TCA9554_REG_OUTPUT,
				       QSFP_OUT_SELECT) != 0) {
			LOG_ERR("QSFP %s: failed to assert ModSelL", c->name);
			status |= (uint32_t)QSFP_ST_READ_FAILED << (8 * i);
			continue;
		}

		id_ret = i2c_reg_read_byte(bus, QSFP_MODULE_I2C_ADDR, CMIS_REG_IDENTIFIER, &id);
		rev_ret = i2c_reg_read_byte(bus, QSFP_MODULE_I2C_ADDR, CMIS_REG_REVISION, &rev);

		/* Release the shared bus for the next cage. */
		(void)i2c_reg_write_byte(bus, c->expander_addr, TCA9554_REG_OUTPUT, QSFP_OUT_IDLE);

		if (id_ret != 0) {
			LOG_WRN("QSFP %s: module seated but 0x50 read failed", c->name);
			status |= (uint32_t)QSFP_ST_READ_FAILED << (8 * i);
			continue;
		}

		status |= (uint32_t)(QSFP_ST_PRESENT_FLAG | (id & 0x7f)) << (8 * i);

		LOG_INF("QSFP %s: module present, id=0x%02x (%s)", c->name, id,
			cmis_identifier_str(id));
		if (rev_ret == 0) {
			LOG_INF("QSFP %s: CMIS rev %u.%u", c->name, rev >> 4, rev & 0x0f);
		}
	}

	return status;
}
