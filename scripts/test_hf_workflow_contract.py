#!/usr/bin/env python3
"""Pin the Hugging Face workflow contract for SZLHOLDINGS/szl-lake.

- The only job that writes the Hub holds the per-asset lock
  ``hf-write/dataset/SZLHOLDINGS/szl-lake`` (never keyed by event or ref) and
  never runs on pull requests.
- Every workflow that installs huggingface_hub pins the same exact version.
- The public-dataset readers (HF -> GitHub sync, anchor re-verification) are
  anonymous: they hold no Hugging Face credential and send no Authorization
  header.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
LOCK = "hf-write/dataset/SZLHOLDINGS/szl-lake"
HUB_PIN = "huggingface_hub==2.0.0"
HUB_REQUIREMENT = re.compile(r"huggingface[_-]hub(?:\[[^\]]*\])?\s*([=<>!~]=?[^\s\"']*)?", re.I)


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as boolean True.
    return workflow.get("on", workflow.get(True)) or {}


class PublisherLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load("hf-sync.yml")
        self.jobs = self.workflow["jobs"]

    def test_only_the_publish_job_holds_the_asset_lock(self) -> None:
        self.assertNotIn("concurrency", self.workflow)
        publish = self.jobs["sync"]
        self.assertEqual(
            publish["concurrency"], {"group": LOCK, "cancel-in-progress": False}
        )
        for name, job in self.jobs.items():
            if name != "sync":
                self.assertNotIn("concurrency", job, name)

    def test_publish_job_never_runs_on_pull_requests_and_needs_the_contract(self) -> None:
        publish = self.jobs["sync"]
        self.assertEqual(publish["needs"], "contract")
        self.assertIn("github.event_name != 'pull_request'", publish["if"])
        self.assertIn("pull_request", triggers(self.workflow))

    def test_publish_step_binds_the_exact_source_revision(self) -> None:
        steps = self.jobs["sync"]["steps"]
        publish = [s for s in steps if "publish_hf_dataset.py" in s.get("run", "")]
        self.assertEqual(len(publish), 1)
        self.assertIn('--source-revision "$GITHUB_SHA"', publish[0]["run"])
        self.assertIn("--report reports/lake-publication.json", publish[0]["run"])

    def test_contract_job_holds_no_hub_credential(self) -> None:
        text = yaml.safe_dump(self.jobs["contract"])
        self.assertNotIn("secrets.", text)
        self.assertNotIn("HF_TOKEN", text)


class HubClientPinTest(unittest.TestCase):
    def test_every_hub_install_uses_the_same_exact_pin(self) -> None:
        seen = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for match in HUB_REQUIREMENT.finditer(path.read_text(encoding="utf-8")):
                if "install" not in match.string[max(0, match.start() - 200):match.start()]:
                    continue
                seen.append((path.name, match.group(0)))
        self.assertTrue(seen)
        for name, requirement in seen:
            self.assertEqual(requirement, HUB_PIN, name)


READERS = ("sync-from-hf.yml", "verify-anchor-receipts.yml")


class AnonymousReaderTest(unittest.TestCase):
    def test_public_dataset_readers_hold_no_hub_credential(self) -> None:
        for name in READERS:
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text(encoding="utf-8")
                self.assertIn("huggingface.co", text)
                self.assertNotIn("HF_TOKEN", text)
                self.assertNotIn("Authorization", text)
                self.assertNotRegex(text, r"secrets\.(HF|HUGGING)")

    def test_reader_pins_one_immutable_revision_and_fails_closed(self) -> None:
        workflow = load("sync-from-hf.yml")
        run = "\n".join(
            step.get("run", "") for step in workflow["jobs"]["sync"]["steps"]
        )
        self.assertIn("^[0-9a-f]{40}$", run)
        self.assertIn("/resolve/$HF_SHA", run)
        commands = [
            line for line in run.splitlines() if not line.strip().startswith("#")
        ]
        self.assertFalse([line for line in commands if "|| true" in line])
        self.assertEqual(
            workflow["concurrency"], {"group": "sync-from-hf", "cancel-in-progress": False}
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
