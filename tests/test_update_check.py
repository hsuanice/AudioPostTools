import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operations import update_check as uc


def release(tag, prerelease=False, draft=False, body="notes"):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "html_url": f"https://example/{tag}", "body": body}


class UpdateCheckTests(unittest.TestCase):
    def test_each_app_sees_only_its_own_newest_published_release(self) -> None:
        releases = [
            release("wmfp-v261121.2103"),
            release("wmfp-v261205.0900", prerelease=True),  # not tested yet
            release("wmfp-v261130.1200", draft=True),
            release("wmfp-v261122.0815"),
            release("wrfp-v261201.1000"),
            release("wmfp-vbroken"),
        ]
        latest = uc.latest_release(releases, "Wave Manager for Post")
        self.assertEqual((latest.tag, latest.version, latest.url), ("wmfp-v261122.0815", "261122.0815", "https://example/wmfp-v261122.0815"))
        self.assertEqual(uc.latest_release(releases, "Wave Repair for Post").version, "261201.1000")

    def test_newer_release_compares_versions_as_numbers(self) -> None:
        releases = [release("wmfp-v261122.0815")]
        self.assertEqual(uc.newer_release(releases, "Wave Manager for Post", "261121.2103").version, "261122.0815")
        self.assertIsNone(uc.newer_release(releases, "Wave Manager for Post", "261122.0815"))
        self.assertIsNone(uc.newer_release(releases, "Wave Manager for Post", "270101.0000"))
        self.assertIsNone(uc.newer_release([], "Wave Manager for Post", "261121.2103"))

    def test_tag_names(self) -> None:
        self.assertEqual(uc.release_tag("Wave Manager for Post", "261121.2103"), "wmfp-v261121.2103")
        self.assertEqual(uc.release_tag("Wave Repair for Post", "261003.1312"), "wrfp-v261003.1312")

    def test_auto_check_once_a_day_and_can_be_turned_off(self) -> None:
        self.assertTrue(uc.auto_check_due({"auto": True, "last_check": 0}, now=100000))
        self.assertFalse(uc.auto_check_due({"auto": True, "last_check": 100000}, now=100000 + 3600))
        self.assertTrue(uc.auto_check_due({"auto": True, "last_check": 100000}, now=100000 + 86400))
        self.assertFalse(uc.auto_check_due({"auto": False, "last_check": 0}, now=100000))

    def test_state_round_trip_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "App" / "update_check.json"
            self.assertEqual(uc.load_state(path), {"auto": True, "last_check": 0})
            uc.save_state(path, {"auto": False, "last_check": 123.0})
            self.assertEqual(uc.load_state(path), {"auto": False, "last_check": 123.0})
            path.write_text("not json")
            self.assertEqual(uc.load_state(path), {"auto": True, "last_check": 0})

    def test_fetch_reports_unreachable_github_readably(self) -> None:
        failed = mock.Mock(returncode=6, stdout="", stderr="curl: (6) Could not resolve host: api.github.com")
        with mock.patch.object(uc.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "Could not reach GitHub .*resolve host"):
                uc.fetch_releases()
        ok = mock.Mock(returncode=0, stdout='[{"tag_name": "wmfp-v261122.0815"}]', stderr="")
        with mock.patch.object(uc.subprocess, "run", return_value=ok) as run:
            self.assertEqual(uc.fetch_releases()[0]["tag_name"], "wmfp-v261122.0815")
        command = run.call_args[0][0]
        self.assertEqual(command[0], "/usr/bin/curl")
        self.assertIn(uc.RELEASES_API, command)


if __name__ == "__main__":
    unittest.main()
