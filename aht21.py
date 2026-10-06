# Minimal MicroPython driver for the AHT21 temperature/humidity sensor.
#
# Usage:
#   from aht21 import AHT21
#   aht = AHT21(i2c)
#   temp_c, rh = aht.read()
#
# How the AHT21 works:
#   1. Send the "trigger measurement" command (0xAC 0x33 0x00).
#   2. Wait ~80 ms while it measures.
#   3. Read 7 bytes: status, 5 data bytes (20-bit humidity + 20-bit temperature
#      packed together), and a CRC byte to check the data arrived intact.

import time

AHT21_ADDR = 0x38


class AHT21:
    def __init__(self, i2c, addr=AHT21_ADDR):
        self.i2c = i2c
        self.addr = addr
        time.sleep_ms(40)  # sensor needs ~40 ms after power-on

        # Status bit 3 = "calibrated". If it's not set, send the init command.
        if not (self._status() & 0x08):
            self.i2c.writeto(self.addr, b"\xBE\x08\x00")
            time.sleep_ms(10)

    def _status(self):
        return self.i2c.readfrom(self.addr, 1)[0]

    @staticmethod
    def _crc8(data):
        # CRC-8, polynomial 0x31, initial value 0xFF (from the datasheet)
        crc = 0xFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        return crc

    def read(self):
        """Measure and return (temperature in °C, relative humidity in %).

        Raises OSError if the sensor stays busy or the data fails the CRC check.
        """
        self.i2c.writeto(self.addr, b"\xAC\x33\x00")
        time.sleep_ms(80)

        # Status bit 7 = busy. Give it a little extra time if needed.
        for _ in range(5):
            data = self.i2c.readfrom(self.addr, 7)
            if not (data[0] & 0x80):
                break
            time.sleep_ms(10)
        else:
            raise OSError("AHT21 busy")

        if self._crc8(data[:6]) != data[6]:
            raise OSError("AHT21 CRC error")

        # Unpack the two 20-bit raw values
        raw_rh = (data[1] << 12) | (data[2] << 4) | (data[3] >> 4)
        raw_t = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]

        rh = raw_rh * 100 / 1048576          # 2^20 = 1048576
        temp_c = raw_t * 200 / 1048576 - 50
        return temp_c, rh
