"""Register images of the four fake inverters (known raw values -> known expected values)."""

from __future__ import annotations

from harness.fakes import serial_words


def s16(v: int) -> int:
    return v & 0xFFFF


def pair(value: int, low: int, high: int, signed: bool = False) -> dict[int, int]:
    raw = value & 0xFFFFFFFF
    return {low: raw & 0xFFFF, high: raw >> 16}


def registers(serial: str, *, soc: int = 55, pv1: int = 4200, role: int = 0, energy_total_raw: int = 34567) -> dict[int, int]:
    regs: dict[int, int] = {
        0: 0x0500, 22: 0x0203, 24: 1, 15: 0x1001, 17: 0x1000, 18: 0x0070, 65: 0x0104,
        500: 2, 551: 1, 553: 0, 554: 0, 555: 0x0001, 556: 0, 557: 0, 558: 0,
        541: 1355, 586: 253, 587: 5120, 588: soc, 591: s16(-150),
        598: 2300, 599: 2310, 600: 2320, 609: 5001,
        672: pv1, 676: 3605, 677: 117, 673: 1800, 678: 3101, 679: 58,
        336: role,
        80: 1, 98: 0, 133: 0, 111: 0,
        # energy counters (raw words; the resolution comes from R65)
        514: 123, 515: 45, 520: 67, 521: 89, 526: 111, 529: 222,
    }
    regs.update(serial_words(serial))
    regs.update(pair(80000, 20, 21))  # rated power 8 kW at 0.1 W
    regs.update(pair(-1500, 625, 690))  # grid selected power (s32)
    regs.update(pair(3200, 636, 694))
    regs.update(pair(70000, 653, 659))
    for low, high, v in ((516, 517, energy_total_raw), (518, 519, 20100), (522, 523, 5000), (524, 525, 6000), (527, 528, 70000), (534, 535, 123456)):
        regs.update(pair(v, low, high))
    return regs


# expected values of the default template for registers() with R65 = 0x0104 (0.1 kWh)
EXPECTED = {
    "battery_temperature": 25.3,
    "battery_voltage": 51.2,
    "battery_soc": 55,
    "battery_current": -1.5,
    "grid_voltage_a": 230.0,
    "grid_voltage_b": 231.0,
    "grid_voltage_c": 232.0,
    "grid_frequency": 50.01,
    "grid_selected_power_total": -1500,
    "inverter_power_total": 3200,
    "load_power_total": 70000,
    "pv1_power": 4200,
    "pv1_voltage": 360.5,
    "pv1_current": 11.7,
    "pv2_power": 1800,
    "pv2_voltage": 310.1,
    "pv2_current": 5.8,
    "temp_heatsink": 35.5,
    "rated_power": 8000.0,
}
