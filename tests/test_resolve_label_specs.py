import json
import os
import pathlib
import subprocess
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "triage" / "scripts" / "resolve-label-specs.py"
LABELS = REPO_ROOT / ".github" / "triage" / "labels.yaml"


class ResolveLabelSpecsTest(unittest.TestCase):
    def run_script(self):
        env = {**os.environ, "TRIAGE_LABELS": str(LABELS)}
        return subprocess.run(
            ["python3", str(SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    def test_emits_label_specs_for_managed_labels(self):
        result = self.run_script()

        self.assertEqual(result.returncode, 0, msg=result.stderr)

        specs = json.loads(result.stdout)
        names = {entry["name"] for entry in specs}

        self.assertIn("regression", names)
        self.assertIn("Priority: Critical", names)
        self.assertIn("ai-triage:needs-review", names)
        self.assertIn("ai-generated-issue", names)

        priority = next(entry for entry in specs if entry["name"] == "Priority: Critical")
        self.assertEqual(priority["color"], "C90000")
        self.assertIn("Production-breaking", priority["description"])

        classification = next(entry for entry in specs if entry["name"] == "needs reprex")
        self.assertEqual(classification["color"], "1D76DB")
        self.assertIn("Missing runnable minimal code", classification["description"])

        review = next(entry for entry in specs if entry["name"] == "ai-triage:needs-review")
        self.assertEqual(review["color"], "FBCA04")

        reporting = next(entry for entry in specs if entry["name"] == "ai-generated-issue")
        self.assertEqual(reporting["color"], "0E8A16")


if __name__ == "__main__":
    unittest.main()
