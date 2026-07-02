from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "test-key")

DOCS_DIR = Path(__file__).resolve().parent
SIMULATOR_DIR = DOCS_DIR.parent / "simulator"
for path in (DOCS_DIR, SIMULATOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from submission_example import MyAgent  # noqa: E402
from env import IFTKEnv  # noqa: E402


class SubmissionExampleGuardsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IFTKEnv(Path(__file__).resolve().parent.parent / "cases")
        self.agent = MyAgent(self.env, model="test-model")

    def test_normalize_office_address_maps_short_building_codes(self) -> None:
        self.assertEqual(self.agent._normalize_office_address("A2"), "0552_A2")
        self.assertEqual(self.agent._normalize_office_address("A4园区"), "0551_A4")
        self.assertIsNone(self.agent._normalize_office_address("小镇园区"))

    def test_select_candidate_room_prefers_last_listed_candidate_match(self) -> None:
        self.agent.last_room_candidates = [
            {"room_id": "A2-1F-147", "officeId": "office-147"},
            {"room_id": "A2-1F-126", "officeId": "office-126"},
        ]
        room = self.agent._select_candidate_room({"room_id": "A2-1F-126"})
        self.assertEqual(room["officeId"], "office-126")

    def test_fallback_project_search_returns_unique_project_from_case_pool(self) -> None:
        self.env.reset("beta_wf_0031")
        projects = self.agent._fallback_project_search()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["project_code"], "A-260100001")


if __name__ == "__main__":
    unittest.main()
