from pathlib import Path
import tempfile
import unittest

from companion.camera_controls import sync_stock_auto_exposure


STOCK = ("[putting]\r\nstartx1 = 184\r\nmjpeg = 1\r\nexposure = 0.0\r\nautoexposure = -1.0\r\n"
         "customhsv = {'hmin': 0, 'smin': 0, 'vmin': 157, 'hmax': 179, 'smax': 255, 'vmax': 255}\r\n\r\n")


class StockAutoExposureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.ini"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text: str) -> None:
        self.path.write_bytes(text.encode("utf-8"))

    def read(self) -> str:
        return self.path.read_bytes().decode("utf-8")

    def test_auto_camera_rewrites_only_the_unreadable_stock_value(self):
        self.write(STOCK)
        self.assertTrue(sync_stock_auto_exposure(self.path, True))
        self.assertEqual(self.read(), STOCK.replace("autoexposure = -1.0", "autoexposure = 1.0"))
        self.assertFalse(sync_stock_auto_exposure(self.path, True))

    def test_manual_camera_keeps_stock_manual_value(self):
        for value in ("-1.0", "0.0"):
            with self.subTest(value=value):
                text = STOCK.replace("-1.0", value)
                self.write(text)
                self.assertFalse(sync_stock_auto_exposure(self.path, False))
                self.assertEqual(self.read(), text)

    def test_camera_switched_to_manual_clears_restored_auto(self):
        self.write(STOCK.replace("-1.0", "1.0"))
        self.assertTrue(sync_stock_auto_exposure(self.path, False))
        self.assertEqual(self.read(), STOCK)

    def test_missing_value_is_added_to_putting_section(self):
        self.write("[other]\nautoexposure = 1\n[putting]\nmjpeg = 1\n")
        self.assertTrue(sync_stock_auto_exposure(self.path, True))
        self.assertEqual(self.read(), "[other]\nautoexposure = 1\n[putting]\nautoexposure = 1.0\nmjpeg = 1\n")

    def test_unreadable_config_is_left_alone(self):
        for text in ("mjpeg = 1\n", "[putting]\nautoexposure = auto\n"):
            with self.subTest(text=text):
                self.write(text)
                self.assertFalse(sync_stock_auto_exposure(self.path, True))
                self.assertEqual(self.read(), text)


if __name__ == "__main__":
    unittest.main()
