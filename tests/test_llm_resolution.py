"""Provider precedence must never silently send interactive work to Claude."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from config import Settings
from profiles.loader import _resolve_llm, load_profile
from agents.llm_config import apply_cli_llm_overrides, create_llm_for_agent


class LLMResolutionTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, llm_provider='openai',
                                 director_llm_provider='', worker_llm_provider='')
        self.patcher = patch('agents.llm_config.get_settings', return_value=self.settings)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_interactive_uses_configured_provider_at_factory(self):
        config = load_profile('interactive').llm
        with patch('agents.llm_config.get_provider') as factory:
            create_llm_for_agent('scanner', config)
        factory.assert_called_once_with('openai')

    def test_role_environment_overrides_global(self):
        self.settings.worker_llm_provider = 'gemini'
        config = _resolve_llm({})
        self.assertEqual(config.default_worker.provider, 'gemini')
        self.assertEqual(config.director.provider, 'openai')

    def test_profile_and_model_only_cli_override(self):
        config = _resolve_llm({'default_worker': {'provider': 'vertex', 'model': 'profile-model'}})
        config = apply_cli_llm_overrides(config, SimpleNamespace(worker_model='cli-model'))
        self.assertEqual(config.default_worker.provider, 'vertex')
        self.assertEqual(config.default_worker.model, 'cli-model')

    def test_provider_change_resets_incompatible_model(self):
        config = _resolve_llm({'default_worker': {'provider': 'claude', 'model': 'claude-model'}})
        config = apply_cli_llm_overrides(config, SimpleNamespace(worker_llm='openai'))
        self.assertEqual(config.default_worker.provider, 'openai')
        self.assertEqual(config.default_worker.model, '')

    def test_partial_profile_inherits_provider_and_named_override(self):
        config = _resolve_llm({'default_worker': {'model': 'custom'},
                               'overrides': {'scanner': {'model': 'scanner-model'}}})
        self.assertEqual(config.get_config('scanner').provider, 'openai')
        self.assertEqual(config.get_config('scanner').model, 'scanner-model')
