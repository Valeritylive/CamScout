"""Logic tests for the SADP-style password reset flow (no device or GTK needed)."""

import os
import sys
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hikvision_api as api


class ExportResetRequestTests(unittest.TestCase):
    def test_builds_request_from_discovery_fields(self):
        data = api.export_reset_request({
            "serialno": "DS-ABC12345",
            "mac": "11:22:33:44:55:66",
            "model": "DS-2CD2043",
            "softwareversion": "V5.6.3",
        })
        root = ET.fromstring(data)
        text = {el.tag.rsplit("}", 1)[-1]: (el.text or "") for el in root}
        self.assertEqual(text["serialNumber"], "DS-ABC12345")
        self.assertEqual(text["macAddress"], "11:22:33:44:55:66")
        self.assertEqual(text["model"], "DS-2CD2043")
        self.assertIn("requestTime", text)

    def test_missing_serial_raises(self):
        with self.assertRaises(api.HikvisionError):
            api.export_reset_request({})

    def test_known_password_uses_guid_export(self):
        with mock.patch.object(api, "export_guid_file", return_value=b"GUID") as guid:
            out = api.export_reset_request({"serialno": "S1"}, address="1.2.3.4",
                                           port=80, username="admin", password="secret123")
        self.assertEqual(out, b"GUID")
        guid.assert_called_once_with("1.2.3.4", 80, "admin", "secret123")


class ResetPasswordWithResponseTests(unittest.TestCase):
    def test_short_password_rejected(self):
        with self.assertRaises(api.HikvisionError):
            api.reset_password_with_response("1.2.3.4", 80, b"<x><GUID>c</GUID></x>", "short")

    def test_empty_response_rejected(self):
        with self.assertRaises(api.HikvisionError):
            api.reset_password_with_response("1.2.3.4", 80, b"   ", "longpass12")

    def test_no_code_in_response_rejected(self):
        with self.assertRaises(api.HikvisionError):
            api.reset_password_with_response("1.2.3.4", 80, b"<root></root>", "longpass12")

    def test_posts_code_and_password_to_device(self):
        captured = {}

        class FakeResponse:
            status_code = 200
            content = b"<ResponseStatus><statusCode>1</statusCode></ResponseStatus>"
            text = ""

        def fake_request(method, url, data=None, headers=None, timeout=None):
            captured["method"] = method
            captured["url"] = url
            captured["data"] = data
            return FakeResponse()

        with mock.patch.object(api.requests, "request", side_effect=fake_request):
            api.reset_password_with_response("1.2.3.4", 80,
                                             b"<Reset><GUID>CODE42</GUID></Reset>", "longpass12")
        body = ET.fromstring(captured["data"])
        values = {el.tag.rsplit("}", 1)[-1]: el.text for el in body}
        self.assertEqual(values["GUID"], "CODE42")
        self.assertEqual(values["password"], "longpass12")
        self.assertIn("/ISAPI/Security/GUID", captured["url"])

    def test_accepts_plain_code_string_response(self):
        class FakeResponse:
            status_code = 200
            content = b"OK"
            text = ""

        with mock.patch.object(api.requests, "request", return_value=FakeResponse()) as req:
            api.reset_password_with_response("1.2.3.4", 80, b"RAWCODE99", "longpass12")
        body = ET.fromstring(req.call_args.kwargs["data"])
        values = {el.tag.rsplit("}", 1)[-1]: el.text for el in body}
        self.assertEqual(values["GUID"], "RAWCODE99")


if __name__ == "__main__":
    unittest.main()
