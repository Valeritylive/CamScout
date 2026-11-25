"""Tests of the parts that join the Dahua and Hikvision sides: languages, brands, columns, CSV."""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dahua_discovery as dd  # noqa: E402
import device_columns as dc  # noqa: E402
import devices  # noqa: E402
import i18n  # noqa: E402
import sadp_discovery  # noqa: E402


def hik_device(**changes):
    values = dict(ip="192.168.1.64", mac="aa-bb-cc-dd-ee-ff", model="DS-2CD2143G2-I", serial="DS-2CD2143G2-I20230101",
                  firmware="V5.7.3", http_port=80, rtsp_port=554, device_port=8000, gateway="192.168.1.1",
                  subnet_mask="255.255.255.0", activated=False)
    values.update(changes)
    return sadp_discovery.Device(**values)


class LanguageTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_default_is_english(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}):
            os.environ.pop("CAMSCOUT_LANG", None)
            self.assertEqual(i18n.init_from_settings(), "en")
            self.assertEqual(i18n.tr("Search"), "Search")

    def test_languages_offered(self):
        self.assertEqual([code for code, _name in i18n.LANGUAGES], ["en", "ru", "de", "es", "hy"])

    def test_switch_and_save(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}):
            os.environ.pop("CAMSCOUT_LANG", None)
            i18n.save_settings({"language": "de"})
            self.assertEqual(i18n.init_from_settings(), "de")
            self.assertEqual(i18n.tr("Search"), "Suchen")
            self.assertEqual(i18n.set_language("xx"), "en")

    def test_catalogs_are_complete(self):
        placeholder = re.compile(r"\{\w*\}")
        keys = None
        for code, _name in i18n.LANGUAGES[1:]:
            with open(ROOT / "locales" / f"{code}.json", encoding="utf-8") as handle:
                catalog = json.load(handle)
            keys = keys or set(catalog)
            self.assertEqual(set(catalog), keys, code)
            for key, value in catalog.items():
                self.assertTrue(value, (code, key))
                self.assertEqual(sorted(placeholder.findall(key)), sorted(placeholder.findall(value)), (code, key))

    def test_backend_messages_follow_language(self):
        i18n.set_language("ru")
        self.assertEqual(devices.status_text(devices.from_sadp(hik_device())), "Неактивно")


class BrandTests(unittest.TestCase):
    def test_from_sadp(self):
        device = devices.from_sadp(hik_device())
        self.assertEqual(device.brand, devices.HIKVISION)
        self.assertEqual(device.mac, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(device.device_class, "IPC")
        self.assertIs(device.initialized, False)
        self.assertEqual(devices.status_text(device), "Inactive")
        self.assertEqual(devices.sadp_record(device).mac, "aa-bb-cc-dd-ee-ff")

    def test_hikvision_class(self):
        self.assertEqual(devices.hikvision_class("DS-7608NI-K2"), "NVR")
        self.assertEqual(devices.hikvision_class("DS-2DE4425IW-DE"), "SD")

    def test_rtsp_paths(self):
        hik = devices.from_sadp(hik_device())
        dahua = dd.Device(ip="192.168.1.108")
        self.assertEqual(devices.rtsp_path(hik, 2, substream=True), "/Streaming/Channels/202")
        self.assertEqual(devices.rtsp_path(dahua), "/cam/realmonitor?channel=1&subtype=0")

    def test_brand_column_and_filter(self):
        hik = devices.from_sadp(hik_device())
        dahua = dd.Device(ip="192.168.1.108", model="IPC-HFW2431S")
        self.assertEqual(dc.COLUMN_KEYS[1], "brand")
        self.assertEqual(dc.column_value(hik, "brand"), "Hikvision")
        self.assertEqual(dc.column_value(dahua, "brand"), "Dahua")
        self.assertTrue(dc.matches_brand(hik, "Hikvision"))
        self.assertFalse(dc.matches_brand(dahua, "Hikvision"))
        self.assertTrue(dc.matches_brand(dahua, "All brands"))

    def test_csv_round_trip(self):
        rows = [devices.from_sadp(hik_device()), dd.Device(ip="192.168.1.108", mac="11:22:33:44:55:66", model="IPC")]
        buffer = io.StringIO()
        self.assertEqual(devices.write_devices_csv(rows, buffer), 2)
        buffer.seek(0)
        back = devices.read_devices_csv(buffer)
        self.assertEqual([(d.brand, d.ip) for d in back], [("Hikvision", "192.168.1.64"), ("Dahua", "192.168.1.108")])


if __name__ == "__main__":
    unittest.main()
