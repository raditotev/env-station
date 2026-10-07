# main.py: desktop air-quality station
#
# MicroPython runs this file automatically every time the Pico powers up.
#
# Every UPDATE_INTERVAL_S seconds it:
#   1. reads temperature and humidity from the AHT21
#   2. passes them to the ENS160 as compensation
#   3. reads AQI, TVOC and eCO2 from the ENS160
#   4. draws everything on the OLED and prints a line to the serial console
#
# While the air is Unhealthy (AQI 5) the board's RGB LED pulses red.
#
# If a sensor (or the display) stops answering, its part of the screen shows
# "ERR" and the program tries to set it up again on the next loop. It never
# exits on a read error. Press Ctrl-C in mpremote to stop it.

import math
import time
import neopixel
from machine import I2C, Pin
from ssd1306 import SSD1306_I2C
from aht21 import AHT21
from ens160 import ENS160, VALID_NORMAL, VALID_WARMUP, VALID_INVALID

# ---------------------------------------------------------------------------
# Settings you might want to change
# ---------------------------------------------------------------------------
UPDATE_INTERVAL_S = 2       # seconds between readings

# The ENS160's heater warms the shared sensor board, so the AHT21 reads high.
# Calibrated 2026-10-07 against a second AHT21 away from the board, averaged
# over 5 minutes once both had settled: board 23.9 C / 47.3 %RH, reference
# 21.1 C / 48.7 %RH. Set both to 0 to see the raw sensor values.
TEMP_OFFSET_C = -2.9
RH_OFFSET = 1.4             # percentage points

I2C_SDA = 4                 # GP4 (pin 6)
I2C_SCL = 5                 # GP5 (pin 7)
I2C_FREQ = 400_000

LED_PIN = 23                # the board's WS2812 RGB LED
LED_PULSE_MS = 2000         # one fade up and down
LED_MAX = 120               # peak red brightness, 0-255

# Air-quality values are shown as words instead of numbers. Each reading has
# its own five words, best to worst, and the four values where the next word
# starts.

# TVOC: German Environment Agency (UBA) indoor levels, converted from ug/m3
# to ppb as in the ENS160 datasheet.
TVOC_WORDS = ("Excellent", "Good", "Moderate", "Poor", "Unhealthy")
TVOC_LIMITS = (65, 220, 660, 2200)     # ppb

# eCO2: UBA CO2 guidance, where 1000 ppm calls for ventilation and 2000 ppm
# is unacceptable. Outdoor air is about 420 ppm.
ECO2_WORDS = ("Excellent", "Good", "Acceptable", "Stuffy", "Poor")
ECO2_LIMITS = (450, 800, 1000, 2000)   # ppm

# The ENS160's own AQI is already 1-5 on the UBA scale: 1 = Excellent.
AQI_WORDS = {i + 1: name for i, name in enumerate(TVOC_WORDS)}


def level(value, limits, words):
    """Turn a number into one of words using limits."""
    for i, limit in enumerate(limits):
        if value < limit:
            return words[i]
    return words[-1]

# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
i2c = I2C(0, sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=I2C_FREQ)

# None means "not set up yet, or it failed": setup_devices() will retry.
oled = None
aht = None
ens = None

# How many reads in a row the ENS160 may report "not measuring" before we
# restart it. It needs about 1 second to start after a restart.
ENS_STALL_READS = 3
ens_stalled = 0

led = neopixel.NeoPixel(Pin(LED_PIN), 1)
led_red = None              # last value written, so we only write on change


def setup_devices():
    """Create any device object that isn't working yet."""
    global oled, aht, ens
    if oled is None:
        try:
            oled = SSD1306_I2C(128, 64, i2c)
        except OSError as e:
            print("OLED setup failed:", e)
    if aht is None:
        try:
            aht = AHT21(i2c)
        except OSError as e:
            print("AHT21 setup failed:", e)
    if ens is None:
        try:
            ens = ENS160(i2c)
        except OSError as e:
            print("ENS160 setup failed:", e)


def correct_temp_rh(temp_c, rh):
    """Apply TEMP_OFFSET_C and RH_OFFSET.

    The humidity reading barely changes with the board's extra warmth, so it
    gets a plain offset rather than being recalculated for the cooler room.
    """
    return temp_c + TEMP_OFFSET_C, max(0.0, min(rh + RH_OFFSET, 100.0))


def read_sensors():
    """Read both sensors. Returns a dict; a value is None if that read failed."""
    global aht, ens, ens_stalled
    r = {"temp": None, "rh": None, "aqi": None, "tvoc": None, "eco2": None,
         "validity": None}

    if aht is not None:
        try:
            r["temp"], r["rh"] = correct_temp_rh(*aht.read())
        except OSError as e:
            print("AHT21 read error:", e)
            aht = None  # set it up again next loop

    if ens is not None:
        try:
            # Only send compensation if we have a fresh temp/humidity reading
            if r["temp"] is not None:
                ens.set_compensation(r["temp"], r["rh"])
            r["aqi"], r["tvoc"], r["eco2"] = ens.read()

            if ens.running():
                ens_stalled = 0
                r["validity"] = ens.validity()
            else:
                # Not measuring: data is all zeros and the validity flag
                # wrongly says OK. Show it as warming up, and if it doesn't
                # start by itself within a few reads, restart it.
                r["validity"] = VALID_WARMUP
                ens_stalled += 1
                if ens_stalled >= ENS_STALL_READS:
                    print("ENS160 not measuring, restarting it")
                    ens.start()
                    ens_stalled = 0
        except OSError as e:
            print("ENS160 read error:", e)
            r["aqi"] = r["tvoc"] = r["eco2"] = r["validity"] = None
            ens = None

    return r


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
# The built-in font is 8x8 pixels: 16 characters per line, 8 lines.
# Layout (y = pixel row of each line):
#   y=0   22.4C      41%RH     temperature left, humidity right
#   y=10  ----------------     divider line
#   y=16  Air     Moderate     overall (ENS160 AQI)
#   y=28  TVOC        Good     chemicals / smells
#   y=40  eCO2  Acceptable     stuffiness (CO2 estimate)
#   y=56  [ WARMING UP... ]    inverted status bar, only when needed

def text_right(s, y):
    """Draw text aligned to the right edge of the screen."""
    oled.text(s, 128 - 8 * len(s), y)


def status_bar(s):
    """White bar across the bottom with black text, centred."""
    oled.fill_rect(0, 55, 128, 9, 1)
    oled.text(s, (128 - 8 * len(s)) // 2, 56, 0)


def draw(r, tick):
    oled.fill(0)

    # --- Top line: temperature and humidity ---
    if r["temp"] is None:
        oled.text("T/RH ERR", 0, 0)
    else:
        oled.text("%.1fC" % r["temp"], 0, 0)
        text_right("%.0f%%RH" % r["rh"], 0)
    oled.hline(0, 10, 128, 1)

    # --- Air quality ---
    warming = r["validity"] is not None and r["validity"] != VALID_NORMAL

    oled.text("Air", 0, 16)
    oled.text("TVOC", 0, 28)
    oled.text("eCO2", 0, 40)
    if r["aqi"] is None:
        words = ("ERR", "ERR", "ERR")
    elif warming:
        # Values aren't meaningful yet, so show dashes instead.
        words = ("--", "--", "--")
    else:
        words = (AQI_WORDS.get(r["aqi"], "?"),
                 level(r["tvoc"], TVOC_LIMITS, TVOC_WORDS),
                 level(r["eco2"], ECO2_LIMITS, ECO2_WORDS))
    text_right(words[0], 16)
    text_right(words[1], 28)
    text_right(words[2], 40)

    # --- Bottom status bar ---
    if r["temp"] is None or r["aqi"] is None:
        status_bar("ERR - retrying")
    elif r["validity"] == VALID_INVALID:
        status_bar("INVALID DATA")
    elif warming:
        dots = "." * (tick % 4)          # animated dots so you can see it's alive
        status_bar("WARMING UP" + dots + " " * (3 - len(dots)))

    oled.show()


# ---------------------------------------------------------------------------
# RGB LED
# ---------------------------------------------------------------------------
def is_critical(r):
    """True when the ENS160 rates the air Unhealthy and isn't warming up."""
    return r["aqi"] == 5 and r["validity"] == VALID_NORMAL


def update_led(pulse):
    """Set the LED for this moment: a red pulse, or off."""
    global led_red
    if pulse:
        phase = time.ticks_ms() % LED_PULSE_MS / LED_PULSE_MS
        fade = (1 - math.cos(2 * math.pi * phase)) / 2
        # Squared, because the eye sees low brightness steps as big jumps
        red = int(LED_MAX * fade * fade)
    else:
        red = 0
    if red != led_red:
        led[0] = (red, 0, 0)
        led.write()
        led_red = red


# ---------------------------------------------------------------------------
# Serial output
# ---------------------------------------------------------------------------
def fmt(value, spec):
    return "ERR" if value is None else spec % value


def print_reading(r):
    if r["temp"] is None or r["aqi"] is None:
        state = "ERR"
    elif r["validity"] == VALID_NORMAL:
        state = "OK"
    elif r["validity"] == VALID_INVALID:
        state = "INVALID"
    else:
        state = "WARMING UP"
    # Serial shows the raw numbers and the words, e.g. "TVOC 120 ppb (Good)"
    if r["aqi"] is None:
        air = "AQI ERR  TVOC ERR  eCO2 ERR"
    else:
        air = "AQI %d (%s)  TVOC %d ppb (%s)  eCO2 %d ppm (%s)" % (
            r["aqi"], AQI_WORDS.get(r["aqi"], "?"),
            r["tvoc"], level(r["tvoc"], TVOC_LIMITS, TVOC_WORDS),
            r["eco2"], level(r["eco2"], ECO2_LIMITS, ECO2_WORDS))
    print("T %s  RH %s  |  %s  |  %s" % (
        fmt(r["temp"], "%.1fC"), fmt(r["rh"], "%.1f%%"), air, state))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
print("Air-quality station starting")
tick = 0
while True:
    start = time.ticks_ms()
    critical = False
    try:
        setup_devices()
        readings = read_sensors()
        critical = is_critical(readings)
        print_reading(readings)
        if oled is not None:
            try:
                draw(readings, tick)
            except OSError as e:
                print("OLED error:", e)
                oled = None
    except Exception as e:
        # Safety net for anything unexpected: report it and keep going.
        # (Ctrl-C raises KeyboardInterrupt, which is not caught here.)
        print("Unexpected error:", repr(e))
    tick += 1

    # Wait out the rest of the interval, animating the LED meanwhile
    while time.ticks_diff(time.ticks_ms(), start) < UPDATE_INTERVAL_S * 1000:
        update_led(critical)
        time.sleep_ms(20)
