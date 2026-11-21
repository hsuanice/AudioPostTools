import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from operations import post_tools_inbox as inbox_mod
from operations.post_tools_inbox import (
    BATCH_SUFFIX, MAX_AGE, SETTLE_SECONDS, WAVE_MANAGER, WAVE_REPAIR, InboxPoller,
    is_running, launch_command, mark_received, parse_batch, ready_batches, register_running, unregister_running,
)



class InboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "Post Tools Inbox"
        self.inbox.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def batch(self, name: str, app: str, files: list[str], age: float = 10.0, action: str = "diagnose") -> Path:
        path = self.inbox / (name + BATCH_SUFFIX)
        path.write_text(f"#app={app}\n#action={action}\n" + "".join(item + "\n" for item in files), encoding="utf-8")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path


class BatchTests(InboxTestCase):
    def test_parse(self) -> None:
        batch = parse_batch(Path("x.batch"), "#app=wave_repair\n#action=diagnose\n/a/1.wav\n\n/a/2.wav\n/a/1.wav\n")
        self.assertEqual((batch.app, batch.action), (WAVE_REPAIR, "diagnose"))
        self.assertEqual(batch.files, [Path("/a/1.wav"), Path("/a/2.wav")])

    def test_only_settled_batches_for_this_app(self) -> None:
        self.batch("1", WAVE_REPAIR, ["/a/1.wav"])
        self.batch("2", WAVE_MANAGER, ["/a/2.wav"])
        self.batch("3", WAVE_REPAIR, ["/a/3.wav"], age=0.0)  # still being written
        ready = ready_batches(WAVE_REPAIR, self.inbox)
        self.assertEqual([batch.files for batch in ready], [[Path("/a/1.wav")]])
        self.assertEqual([batch.files for batch in ready_batches(WAVE_MANAGER, self.inbox)], [[Path("/a/2.wav")]])

    def test_settles_after_it_stops_growing(self) -> None:
        path = self.batch("1", WAVE_REPAIR, ["/a/1.wav"], age=0.0)
        now = path.stat().st_mtime
        self.assertEqual(ready_batches(WAVE_REPAIR, self.inbox, now=now + SETTLE_SECONDS - 0.1), [])
        self.assertEqual(len(ready_batches(WAVE_REPAIR, self.inbox, now=now + SETTLE_SECONDS + 0.1)), 1)

    def test_expired_batches_are_put_away_unopened(self) -> None:
        self.batch("old", WAVE_REPAIR, ["/a/1.wav"], age=MAX_AGE + 5)
        self.assertEqual(ready_batches(WAVE_REPAIR, self.inbox), [])
        self.assertEqual([item.name for item in (self.inbox / "Received").iterdir()], ["old (expired).batch"])

    def test_paused(self) -> None:
        self.batch("1", WAVE_REPAIR, ["/a/1.wav"])
        (self.inbox / "Paused").write_text("")
        self.assertEqual(ready_batches(WAVE_REPAIR, self.inbox), [])
        (self.inbox / "Paused").unlink()
        self.assertEqual(len(ready_batches(WAVE_REPAIR, self.inbox)), 1)

    def test_received_keeps_only_the_latest(self) -> None:
        with mock.patch.object(inbox_mod, "KEEP_RECEIVED", 3):
            for index in range(5):
                path = self.batch(f"b{index}", WAVE_REPAIR, ["/a.wav"])
                mark_received(parse_batch(path, path.read_text()), self.inbox)
                stamp = time.time() - 100 + index
                os.utime(self.inbox / "Received" / path.name, (stamp, stamp))
        self.assertEqual(sorted(item.name for item in (self.inbox / "Received").iterdir()), ["b2.batch", "b3.batch", "b4.batch"])
        self.assertEqual(list(self.inbox.glob("*.batch")), [])


class FakeRoot:
    def __init__(self) -> None:
        self.calls = []

    def after(self, _ms, callback):
        self.calls.append(callback)
        return f"after#{len(self.calls)}"

    def after_cancel(self, _ident) -> None:
        pass


class PollerTests(InboxTestCase):
    def test_busy_app_gets_the_batch_again_later(self) -> None:
        self.batch("1", WAVE_REPAIR, ["/a/1.wav"])
        answers = [False, True]
        seen = []

        def handle(batch):
            seen.append(batch.files)
            return answers.pop(0)

        with mock.patch.object(inbox_mod, "is_running", return_value=False):
            poller = InboxPoller(FakeRoot(), WAVE_REPAIR, handle, inbox=self.inbox)
        poller.check()
        self.assertTrue((self.inbox / "1.batch").exists())
        poller.check()
        self.assertFalse((self.inbox / "1.batch").exists())
        self.assertEqual(len(seen), 2)
        poller.stop()
        self.assertFalse((self.inbox / f".running-{WAVE_REPAIR}").exists())

    def test_a_failing_handler_does_not_stop_polling(self) -> None:
        self.batch("1", WAVE_REPAIR, ["/a/1.wav"])
        root = FakeRoot()
        poller = InboxPoller(root, WAVE_REPAIR, lambda batch: 1 / 0, inbox=self.inbox)
        count = len(root.calls)
        poller._tick()
        self.assertEqual(len(root.calls), count + 1)
        poller.stop()


class RunningTests(InboxTestCase):
    def test_registered_process_counts_only_while_alive_and_matching(self) -> None:
        self.assertFalse(is_running(WAVE_REPAIR, self.inbox))
        register_running(WAVE_REPAIR, self.inbox)
        # This test process isn't Wave Repair — unless its command line says so.
        self.assertFalse(is_running(WAVE_REPAIR, self.inbox))
        own_command = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "command="], capture_output=True, text=True).stdout.strip()
        with mock.patch.dict(inbox_mod.APPS, {WAVE_REPAIR: ("Wave Repair for Post", own_command[-12:])}):
            self.assertTrue(is_running(WAVE_REPAIR, self.inbox))
        (self.inbox / f".running-{WAVE_REPAIR}").write_text("999999")
        self.assertFalse(is_running(WAVE_REPAIR, self.inbox))
        unregister_running(WAVE_REPAIR, self.inbox)  # someone else's note: left alone
        self.assertTrue((self.inbox / f".running-{WAVE_REPAIR}").exists())

    def test_launch_command(self) -> None:
        self.assertEqual(launch_command(WAVE_MANAGER, frozen=True), ["open", "-a", "Wave Manager for Post"])
        command = launch_command(WAVE_REPAIR, python="/py", app_dir=Path("/apps"))
        self.assertEqual(command, ["/py", "/apps/wave_repair.py"])


if __name__ == "__main__":
    unittest.main()
