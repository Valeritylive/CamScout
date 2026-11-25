"""Static checks for GTK4 API misuse that only shows up when a dialog opens."""

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app.py"


class GtkUsageTests(unittest.TestCase):
    def setUp(self):
        self.source = APP.read_text(encoding="utf-8")

    def test_password_entry_has_no_set_placeholder_text(self):
        # Gtk.PasswordEntry has a "placeholder-text" property but no setter method.
        names = set(re.findall(r"(\w+)\s*=\s*(?:Gtk\.PasswordEntry\(|password_entry\()", self.source))
        bad = [name for name in names if re.search(rf"\b{name}\.set_placeholder_text\(", self.source)]
        self.assertEqual(bad, [], "use set_property('placeholder-text', ...) on Gtk.PasswordEntry")

    def test_no_gtk3_only_calls(self):
        for call in (".pack_start(", ".show_all(", ".get_content_area(", "Gtk.Dialog("):
            self.assertNotIn(call, self.source)

    def test_versions_required_before_import(self):
        self.assertLess(self.source.index('require_version("Gdk", "4.0")'),
                        self.source.index("from gi.repository import"))


if __name__ == "__main__":
    unittest.main()
