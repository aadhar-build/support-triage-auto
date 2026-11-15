import unittest


class TestPackage(unittest.TestCase):
    def test_imports(self):
        import triage  # noqa: F401
