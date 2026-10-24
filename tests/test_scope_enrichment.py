import unittest

from agents.pipeline import AgentPipeline, _looks_like_rule
from models.program import BountyProgram

# A program page that interleaves an in-scope list, unrelated vendor domains,
# host exclusions, PDF pagination/date noise, and a vuln-class rule — the shape
# that previously polluted scope and filed real rules as hosts.
SCOPE_TEXT = """
Focus assets in scope
*.t-mobile.com
sprint.com

Out of scope
*.sprint.net
/self-service-*
vendor-adtech.example
52/66
3/20/26, 6:08 PM
Name / Location
Cross-Site Scripting without a working proof of concept.
"""


class ManualScopeFreezeTests(unittest.TestCase):
    def test_manual_program_scope_is_not_widened_by_page_parsing(self):
        prog = AgentPipeline._build_program_from_targets(
            ["t-mobile.com", "sprint.com"],
            "https://bugcrowd.com/engagements/t-mobile",
        )
        AgentPipeline._parse_scope_text(prog, SCOPE_TEXT)
        # The user's --targets stay authoritative; parsed in-scope hosts and the
        # unrelated vendor domain never widen the set the swarm may reach.
        self.assertEqual(sorted(prog.all_domains), ["sprint.com", "t-mobile.com"])
        self.assertNotIn("vendor-adtech.example", prog.all_domains)

    def test_platform_program_still_enriches_in_scope(self):
        prog = BountyProgram(name="Acme", platform="bugcrowd",
                             url="https://bugcrowd.com/acme",
                             scope_domains=["acme.com"], active=True)
        AgentPipeline._parse_scope_text(prog, SCOPE_TEXT)
        # Non-manual programs keep the existing enrichment behavior.
        self.assertIn("*.t-mobile.com", prog.all_domains)


class OutOfScopeNoiseTests(unittest.TestCase):
    def test_host_and_path_exclusions_kept_noise_dropped(self):
        prog = AgentPipeline._build_program_from_targets(["t-mobile.com"], None)
        AgentPipeline._parse_scope_text(prog, SCOPE_TEXT)
        self.assertIn("*.sprint.net", prog.out_of_scope)
        self.assertIn("/self-service-*", prog.out_of_scope)
        self.assertIn("vendor-adtech.example", prog.out_of_scope)
        # PDF footers / dates / column headers must not land in the host list.
        self.assertNotIn("52/66", prog.out_of_scope)
        self.assertNotIn("Name / Location", prog.out_of_scope)
        # A vuln-class rule is a rule, not a host.
        self.assertFalse(any("Cross-Site Scripting" in e for e in prog.out_of_scope))
        self.assertTrue(any("Cross-Site Scripting" in e for e in prog.excluded_vuln_types))

    def test_looks_like_rule_discriminates_prose_from_noise(self):
        self.assertTrue(_looks_like_rule("Clickjacking on pages with no sensitive data."))
        self.assertFalse(_looks_like_rule("52/66"))
        self.assertFalse(_looks_like_rule("3/20/26, 6:08 PM"))
        self.assertFalse(_looks_like_rule("Tags"))


if __name__ == "__main__":
    unittest.main()
