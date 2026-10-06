# Minimal MicroPython driver for the ScioSense ENS160 air-quality sensor.
#
# Usage:
#   from ens160 import ENS160
#   ens = ENS160(i2c)                  # sets STANDARD operating mode
#   ens.set_compensation(22.5, 45.0)   # temp °C, humidity %
#   aqi, tvoc, eco2 = ens.read()
#   ens.validity()                     # 0 = data valid, see VALIDITY_TEXT
#
# All multi-byte registers are little-endian (low byte first).

import time

ENS160_ADDR = 0x53  # 0x52 if the ADD pin is pulled low

# Registers
REG_PART_ID = 0x00     # 2 bytes, should read 0x0160
REG_OPMODE = 0x10
REG_TEMP_IN = 0x13     # 2 bytes, compensation temperature
REG_RH_IN = 0x15       # 2 bytes, compensation humidity
REG_STATUS = 0x20
REG_DATA_AQI = 0x21
REG_DATA_TVOC = 0x22   # 2 bytes, ppb
REG_DATA_ECO2 = 0x24   # 2 bytes, ppm

# Operating modes
MODE_IDLE = 0x01
MODE_STANDARD = 0x02

# Validity flag (status bits 3:2)
VALID_NORMAL = 0
VALID_WARMUP = 1         # first ~3 minutes after power-on
VALID_INITIAL = 2        # first ~1 hour of the sensor's life
VALID_INVALID = 3
VALIDITY_TEXT = ("OK", "warm-up", "initial start-up", "invalid")


class ENS160:
    def __init__(self, i2c, addr=ENS160_ADDR):
        self.i2c = i2c
        self.addr = addr

        part_id = int.from_bytes(self._read(REG_PART_ID, 2), "little")
        if part_id != 0x0160:
            raise OSError("ENS160 not found (part id 0x%04x)" % part_id)

        # Changing mode restarts the ~3 minute warm-up, so only start the
        # sensor if it isn't already measuring (e.g. after a Pico reset while
        # the sensor stayed powered).
        if self._read(REG_OPMODE, 1)[0] != MODE_STANDARD or not self.running():
            self.start()

    def start(self):
        """Go through IDLE to STANDARD (gas sensing) mode.

        The sensor needs about 1 second before running() turns True, then
        runs its ~3 minute warm-up.
        """
        self._write(REG_OPMODE, bytes([MODE_IDLE]))
        time.sleep_ms(50)
        self._write(REG_OPMODE, bytes([MODE_STANDARD]))
        time.sleep_ms(50)

    def running(self):
        """True if the sensor is actually measuring (status bit 7, STATAS).

        The mode register can say STANDARD while the sensor is stuck and not
        measuring; in that state all data reads 0.
        """
        return bool(self.status() & 0x80)

    def _read(self, reg, n):
        return self.i2c.readfrom_mem(self.addr, reg, n)

    def _write(self, reg, data):
        self.i2c.writeto_mem(self.addr, reg, data)

    def set_compensation(self, temp_c, rh):
        """Tell the ENS160 the ambient temperature and humidity.

        The sensor wants temperature in Kelvin x 64 and humidity in % x 512.
        """
        t = int((temp_c + 273.15) * 64)
        h = int(rh * 512)
        self._write(REG_TEMP_IN, t.to_bytes(2, "little") + h.to_bytes(2, "little"))

    def status(self):
        """Raw DEVICE_STATUS byte."""
        return self._read(REG_STATUS, 1)[0]

    def validity(self):
        """0 = normal, 1 = warm-up, 2 = initial start-up, 3 = invalid."""
        return (self.status() >> 2) & 0x03

    def read(self):
        """Return (AQI 1-5, TVOC ppb, eCO2 ppm)."""
        data = self._read(REG_DATA_AQI, 5)  # AQI, TVOC (2), eCO2 (2) in one go
        aqi = data[0] & 0x07
        tvoc = data[1] | (data[2] << 8)
        eco2 = data[3] | (data[4] << 8)
        return aqi, tvoc, eco2
