
import unittest
from typing import Any, Dict, Set
from unittest import mock

from tools.testing.target_determination.heuristics.interface import TestPrioritizations

class TestTestPrioritizations(unittest.TestCase):

    def test_when_integrating_high_priority_tests_then_tests_dont_get_duplicated(self):
        tests = ["test1", "test2", "test3", "test4", "test5"]
        orig_prioritization = TestPrioritizations(unranked_relevance=tests.copy())

        heuristic_results = TestPrioritizations(high_relevance=["test1"])

        orig_prioritization.integrate_priorities(heuristic_results)

        expected_high_relevance=["test1"]
        exunranked_relevance=["test2", "test3", "test4", "test5"])

        self.assertListEqual(expected_results.highly_relevant, orig_prioritization.highly_relevant)
        self.assertListEqual(expected_results.probably_relevant, orig_prioritization.probably_relevant)
        self.assertListEqual(expected_results.unranked_relevance, orig_prioritization.unranked_relevance)
        pass

if __name__ == "__main__":
    unittest.main()
