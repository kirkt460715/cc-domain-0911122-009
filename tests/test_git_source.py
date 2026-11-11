import tempfile,unittest
from pathlib import Path
from lab.git_source import build,git
class GitSource(unittest.TestCase):
    def test_history_and_divergence(self):
        with tempfile.TemporaryDirectory() as d:
            x=build(d);p=Path(x['publisher'])
            self.assertEqual(len(git(p,'rev-list','--first-parent','main').splitlines()),5)
            self.assertEqual(len(git(p,'rev-list','--parents','-n','1',x['merged']).split()),3)
            self.assertEqual(git(p,'merge-base',x['deleted'],x['rewritten']),x['first'])
            self.assertNotIn('pump',git(p,'ls-tree','-r','--name-only','main'))
