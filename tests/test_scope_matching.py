import unittest

from utils.scope import host_in_scope


class WildcardScopeMatchingTests(unittest.TestCase):
    """Regression test for a real bug found during a live validation run
    (2026-10-23): BountyProgram.all_domains stores wildcard scope entries
    with the literal "*." prefix intact (platforms/hackerone.py etc.), but
    the scope checks in agents/tool_agent.py and memory/shared.py compared
    against that literal string — "*.elastic.co" can never suffix-match a
    real hostname, so every subdomain a wildcard was meant to cover got
    blocked as out-of-scope. Only bare, non-wildcard scope entries worked.
    """

    ELASTIC_SCOPE = {
        "*.ela.st", "*.elastic.co", "*.elastic.dev", "*.elastic.wtf",
        "*.elasticacademy.com", "*.elasticaccelerationzone.co",
        "*.elasticapm.co", "*.elasticbeats.wtf",
    }

    def test_wildcard_covers_subdomains(self):
        for host in ("academy.elastic.co", "api.elastic.co", "app.elastic.dev",
                     "track.email.elastic.co", "govcloud-status.elastic.co"):
            self.assertTrue(host_in_scope(host, self.ELASTIC_SCOPE), host)

    def test_wildcard_does_not_cover_bare_apex(self):
        # Programs commonly scope it exactly this way on purpose (wildcard
        # eligible, apex marketing site not) — collapsing the two would
        # silently waive a real scope restriction, not just fix a false block.
        self.assertFalse(host_in_scope("elastic.co", self.ELASTIC_SCOPE))
        self.assertFalse(host_in_scope("elastic.dev", self.ELASTIC_SCOPE))

    def test_unrelated_hosts_stay_out_of_scope(self):
        for host in ("crt.sh", "evil.com", "notelastic.co"):
            self.assertFalse(host_in_scope(host, self.ELASTIC_SCOPE), host)

    def test_bare_scope_entry_matches_itself_and_subdomains(self):
        allowed = {"example.com"}
        self.assertTrue(host_in_scope("example.com", allowed))
        self.assertTrue(host_in_scope("api.example.com", allowed))
        self.assertFalse(host_in_scope("evil.com", allowed))

    def test_bare_and_wildcard_entries_can_coexist(self):
        # A program can list both the apex AND a wildcard separately —
        # then the apex IS in scope too, via its own bare entry.
        allowed = {"example.com", "*.example.com"}
        self.assertTrue(host_in_scope("example.com", allowed))
        self.assertTrue(host_in_scope("api.example.com", allowed))


if __name__ == "__main__":
    unittest.main()
