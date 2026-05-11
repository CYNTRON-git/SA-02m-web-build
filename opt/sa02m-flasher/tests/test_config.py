from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sa02m_flasher import config


class TestConfig(unittest.TestCase):
    def test_load_serial_map_for_2eth_board(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "sa02m_flasher.conf"
            serial_map_path = Path(td) / "sa02m_serial_map.conf"
            conf_path.write_text("", encoding="utf-8")
            serial_map_path.write_text(
                "\n".join(
                    [
                        "SA02M_SERIAL_PROFILE=sa02m-2eth",
                        "SA02M_SERIAL_COUNT=4",
                        "SA02M_TTY_0=ttyS3",
                        "SA02M_TTY_1=ttyS4",
                        "SA02M_TTY_2=ttyS5",
                        "SA02M_TTY_3=ttyS7",
                        "SA02M_RS485_0=RS-485-0",
                        "SA02M_RS485_1=RS-485-1",
                        "SA02M_RS485_2=RS-485-2",
                        "SA02M_RS485_3=RS-485-3",
                    ]
                ),
                encoding="utf-8",
            )

            original_path = config.DEFAULT_SERIAL_MAP_PATH
            try:
                config.DEFAULT_SERIAL_MAP_PATH = serial_map_path
                cfg = config.load_config(conf_path)
            finally:
                config.DEFAULT_SERIAL_MAP_PATH = original_path

        self.assertEqual(
            cfg.ports_map,
            {
                "COM1": "/dev/COM1",
                "COM2": "/dev/COM2",
                "COM3": "/dev/COM3",
                "COM4": "/dev/COM4",
            },
        )
        self.assertEqual(
            cfg.ports_labels,
            {
                "COM1": "RS-485-0",
                "COM2": "RS-485-1",
                "COM3": "RS-485-2",
                "COM4": "RS-485-3",
            },
        )


if __name__ == "__main__":
    unittest.main()
