"""The apps' message dialogs are drawn by Tk instead of tkinter.messagebox
(a native macOS alert crashes Tk when a game controller is paired). These
check that they answer exactly like tkinter.messagebox."""
import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ui_components
from ui_components import messagebox, show_dialog


def press(sequence):
    """Replace wait_window so the dialog gets `sequence` as soon as it shows."""
    original = tk.Misc.wait_window

    def fake(self, window=None):
        target = window or self
        target.after(50, lambda: target.event_generate(sequence))
        return original(self, window)

    return mock.patch.object(tk.Misc, "wait_window", fake)


class TkDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"no display: {exc}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.root.destroy()

    def test_return_values_match_tkinter_messagebox(self) -> None:
        with press("<Return>"):
            self.assertEqual(messagebox.showinfo("T", "m", parent=self.root), "ok")
            self.assertEqual(messagebox.showwarning("T", "m", parent=self.root), "ok")
            self.assertEqual(messagebox.showerror("T", "m", parent=self.root), "ok")
            self.assertIs(messagebox.askyesno("T", "m", parent=self.root), True)
            self.assertIs(messagebox.askokcancel("T", "m", parent=self.root), True)
            self.assertIs(messagebox.askyesno("T", "m", parent=self.root, default="no"), False)
        with press("<Escape>"):
            self.assertIs(messagebox.askyesno("T", "m", parent=self.root), False)
            self.assertIs(messagebox.askokcancel("T", "m", parent=self.root), False)
            self.assertEqual(messagebox.showinfo("T", "m", parent=self.root), "ok")

    def test_custom_buttons_and_long_text(self) -> None:
        long_text = "\n".join(f"line {index}" for index in range(60))
        with press("<Return>"):
            self.assertEqual(show_dialog("T", "lead", ("Later", "Open Download Page"), cancel="Later", parent=self.root, detail=long_text), "Open Download Page")
            self.assertEqual(messagebox.showinfo("T", long_text, parent=self.root), "ok")
        with press("<Escape>"):
            self.assertEqual(show_dialog("T", "lead", ("Later", "Open Download Page"), cancel="Later", parent=self.root), "Later")

    def test_apps_never_use_the_native_alert(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in ("wave_manager.py", "wave_repair.py", "ui_components.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("from tkinter import filedialog, messagebox", text, name)
            self.assertNotIn("tkinter.messagebox.", text.replace("# tkinter.messagebox", ""), name)
        self.assertIs(sys.modules["ui_components"].messagebox, ui_components.messagebox)


if __name__ == "__main__":
    unittest.main()
