"""Release archives contain only reviewed inputs and reject altered bundles."""
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_pilot import ROOT,PREFIX,build,sources,verify
from othryss.fixture import build_explorer


class PilotPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"source";self.root.mkdir()
        for relative in set(sources().values()):
            target=self.root/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/relative,target)

    def test_only_allowlisted_files_are_shipped_and_build_is_repeatable(self):
        for name in ("local.env","private.pem","alerts.local.json","ops.local.json","artifacts/history/data.sqlite","othryss/private_notes.py"):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text("PRIVATE-CANARY")
        one=Path(self.temp.name)/"one.zip";two=one.with_name("two.zip")
        build(self.root,one);build(self.root,two)
        self.assertEqual(one.read_bytes(),two.read_bytes())
        manifest=verify(one)
        with zipfile.ZipFile(one) as archive:
            self.assertFalse(any(b"PRIVATE-CANARY" in archive.read(name) for name in archive.namelist()))
            fixture=json.loads(archive.read(f"{PREFIX}/fixtures/incentives/partial-fill-and-exit.json"))
            self.assertEqual({e["instrument_id"] for e in fixture["events"]},{"PILOT-EXAMPLE"})
        self.assertEqual(manifest["example"],"synthetic")
        result=build_explorer(self.root/"fixtures/pilot/partial-fill-and-exit.json",replays=2)
        self.assertEqual(result["fill_count"],2)
        self.assertTrue(result["fixture"]["synthetic"])
        self.assertEqual(result["totals"]["net_cash_flow"],"-0.10")
        self.assertEqual(result["duplicates_skipped"],5)
        with self.assertRaises(ValueError):build(self.root,one)

    def test_secret_in_reviewed_input_fails_before_writing_archive(self):
        with (self.root/"othryss/ops.py").open("a") as out:
            out.write("\n# -----BEGIN PRIVATE KEY-----\n")
        target=Path(self.temp.name)/"rejected.zip"
        with self.assertRaisesRegex(ValueError,"Private key material"):build(self.root,target)
        self.assertFalse(target.exists())

    def test_changed_content_and_unexpected_paths_are_rejected(self):
        original=Path(self.temp.name)/"original.zip";build(self.root,original)
        for mode in ("tamper","extra"):
            target=original.with_name(mode+".zip")
            with zipfile.ZipFile(original) as source,zipfile.ZipFile(target,"w") as out:
                for entry in source.infolist():
                    data=source.read(entry.filename)
                    if mode=="tamper" and entry.filename.endswith("/web/app.js"):data+=b"changed"
                    out.writestr(entry,data)
                if mode=="extra":out.writestr(f"{PREFIX}/../escape.txt",b"bad")
            with self.assertRaises(ValueError):verify(target)


if __name__=="__main__":unittest.main()
