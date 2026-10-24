import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from agents.swarm_pipeline import SwarmPipeline


class PipelineControlTests(unittest.TestCase):
    def test_group_controls_forward_to_group_and_shared_pause_event(self):
        pipeline = SwarmPipeline.__new__(SwarmPipeline)
        pipeline._team = None
        pipeline._group = Mock()
        pipeline._pause_event = asyncio.Event()
        pipeline.pause()
        self.assertFalse(pipeline._pause_event.is_set())
        pipeline._group.pause.assert_called_once()
        pipeline.resume()
        self.assertTrue(pipeline._pause_event.is_set())
        pipeline._group.resume.assert_called_once()
        pipeline.stop()
        self.assertTrue(pipeline._stop_flag)
        pipeline._group.stop.assert_called_once()

    def test_budget_updates_all_workers_and_validator_in_both_topologies(self):
        for grouped in (False, True):
            pipeline = SwarmPipeline.__new__(SwarmPipeline)
            teams = [SimpleNamespace(pentesters=[SimpleNamespace(session_budget=10)])
                     for _ in range(2 if grouped else 1)]
            validator = SimpleNamespace(session_budget=10)
            pipeline._team = None if grouped else teams[0]
            pipeline._group = SimpleNamespace(teams=teams, auditor=validator) if grouped else None
            pipeline.validator = None if grouped else validator
            pipeline.effective_budget = 3
            self.assertEqual(pipeline.effective_budget, 3)
            for team in teams:
                self.assertEqual(team.effective_budget, 3)
                self.assertEqual(team.pentesters[0].session_budget, 3)
            self.assertEqual(validator.session_budget, 3)
            if grouped:
                self.assertEqual(pipeline._group.effective_budget, 3)
