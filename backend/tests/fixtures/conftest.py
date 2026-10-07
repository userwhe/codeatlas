"""Fixture repositories and pull requests are data served by the fake GitHub gateway. Their test
files belong to the fixture apps and never run here.
"""

collect_ignore_glob = ["repos/*", "pull-requests/*"]
