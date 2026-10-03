"""Live OBIS map and scaler-unit. Unit 30 is Wh (IEC 62056-6-2)."""

import unittest

from sa02m_spodes.axdr import i8, structure, u16, u32, u8
from sa02m_spodes.mercury import read_power
from sa02m_spodes.obis_spodes import ENERGY, LIVE, POWER, physical
from sa02m_spodes.xdlms import CosemAccessError

VOLTAGE_A = (1, 0, 32, 7, 0, 255)


class MapTest(unittest.TestCase):
    def test_scaler_unit_30_is_watt_hours(self):
        # 1 234 567 × 10^-2, unit 30. The plan text says "kWh"; IEC unit 30
        # is Wh, and the CE card divides Wh by 1000. The magnitude is 12345.67.
        mag, unit = physical(1_234_567, -2, 30)
        self.assertAlmostEqual(mag, 12345.67)
        self.assertEqual(unit, "Wh")

    def test_phase_voltage_maps_to_voltage_a(self):
        self.assertEqual(POWER["voltage_a"], VOLTAGE_A)
        self.assertIn("voltage_a", LIVE)

    def test_tariffs_1_to_4_are_not_live(self):
        for name, code in ENERGY.items():
            self.assertEqual(code[4], 0, name)
        for tariff in range(1, 5):
            self.assertNotIn((1, 0, 1, 8, tariff, 255), LIVE.values())

    def test_missing_obis_does_not_abort(self):
        class Session:
            def get(self, class_id, obis, attr):
                if tuple(obis) == VOLTAGE_A and attr == 2:
                    return u32(2300)
                if tuple(obis) == VOLTAGE_A and attr == 3:
                    return structure([i8(-1), u8(35)])
                raise CosemAccessError(9)

        got = read_power(Session(), {})
        self.assertEqual(set(got), {"voltage_a"})
        mag, unit = got["voltage_a"]
        self.assertAlmostEqual(mag, 230.0)
        self.assertEqual(unit, "V")


if __name__ == "__main__":
    unittest.main()
