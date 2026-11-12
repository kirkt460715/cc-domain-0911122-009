import json
import os
import pathlib
import subprocess
import tempfile
import unittest

from corpus_gate.service import CorpusGate
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate.ingest.gitcollect import engine, evidence, store
from corpus_gate.errors import GateError

AUTH = {'purposes': ['research'], 'regions': ['cn'],
        'valid_from': '2026-01-01T00:00:00Z', 'valid_until': '2027-01-01T00:00:00Z',
        'obligations': [], 'allow_export': True}
COMMIT_ENV = {**os.environ,
              'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@local.invalid',
              'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@local.invalid',
              'GIT_AUTHOR_DATE': '2026-11-02T00:00:00Z',
              'GIT_COMMITTER_DATE': '2026-11-02T00:00:00Z'}


def bare_update(remote, ref, oid):
    subprocess.run(['git', '--git-dir=' + str(remote), 'update-ref', ref, oid],
                   check=True, env=COMMIT_ENV)


class GitCollectCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.fx = __import__('lab.git_source', fromlist=['build']).build(self._tmp.name)
        self.mirror_dir = str(self.tmp / 'mirrors')
        self.app = CorpusGate()
        self.db = self.app.db
        sources.create(self.db, 'manuals', AUTH)
        profiles.put(self.db, 'default', 0, {})
        self.eid = 'manuals-git'

    def tearDown(self):
        self._tmp.cleanup()
        self.app.close()

    def register_and_collect(self):
        engine.register(self.db, self.eid, self.fx['remote'], 'main', 'manuals',
                        'manuals', 'default', self.mirror_dir)
        engine.freeze(self.db, self.eid)
        return engine.collect(self.db, self.eid)

    def test_commit_to_document_mapping_rename_modify_merge_delete(self):
        result = self.register_and_collect()
        # 第一父链 5 个提交全部确认，合并提交保留双父但侧支只在合并点导一次。
        self.assertEqual(result['chain_length'], 5)
        self.assertTrue(result['reached_head'])
        by_commit = {c['commit']: c for c in result['commits_processed']}
        moved = by_commit[self.fx['moved']]
        self.assertEqual(moved['changes'], [
            {'doc_id': 'pump-1', 'kind': 'renamed_modified', 'version': 2,
             'path': 'manuals/pump-renamed.md', 'previous_path': 'manuals/pump.md'}])
        merged = by_commit[self.fx['merged']]
        self.assertTrue(merged['is_merge'])
        self.assertEqual(len(merged['parents']), 2)
        # 阀门文档只在合并点出现一次（侧支临时版本不重放）。
        self.assertEqual([c['doc_id'] for c in merged['changes']], ['valve-1'])
        deleted = by_commit[self.fx['deleted']]
        self.assertEqual(deleted['changes'][0]['kind'], 'withdrawn')
        self.assertEqual(deleted['changes'][0]['doc_id'], 'pump-1')

    def test_doc_identity_is_doc_id_and_path_is_only_location(self):
        self.register_and_collect()
        tl = evidence.document_timeline(self.db, self.eid, 'pump-1')
        # 同一 doc_id 跨重命名形成连续版本历史，版本号单调。
        self.assertEqual([(v['version'], v['path']) for v in tl['versions']],
                         [(1, 'manuals/pump.md'), (2, 'manuals/pump-renamed.md')])
        statuses = [(s['status'], s['path']) for s in tl['timeline']]
        self.assertIn(('withdrawn', None), statuses)
        # 版本证据可由本地裸镜像独立重算核对。
        ev = evidence.verify_blob_evidence(self.db, self.eid, 'pump-1', 1)
        self.assertTrue(ev['matches'])
        self.assertEqual(ev['blob_type'], 'blob')

    def test_head_tree_marks_withdrawn_not_visible(self):
        self.register_and_collect()
        head = evidence.commit_map(self.db, self.eid)
        docs = {d['doc_id']: d['status'] for d in head['documents']}
        self.assertEqual(docs['pump-1'], 'withdrawn')
        self.assertEqual(docs['index-1'], 'visible')
        self.assertEqual(docs['valve-1'], 'visible')

    def test_resume_is_idempotent_and_checkpoint_points_to_confirmed_tip(self):
        first = self.register_and_collect()
        cp = evidence.checkpoint(self.db, self.eid)
        self.assertEqual(cp['confirmed_tip'], self.fx['deleted'])
        self.assertTrue(cp['resumable'])
        again = engine.collect(self.db, self.eid)
        self.assertEqual(again['commits_processed'], [])
        # 两次采集确认集合一致。
        self.assertEqual(evidence.checkpoint(self.db, self.eid)['confirmed_count'],
                         first['chain_length'])

    def test_duplicate_doc_id_conflict_is_recorded_and_skipped(self):
        publisher = pathlib.Path(self.fx['publisher'])
        (publisher / 'manuals' / 'dup2.md').write_text('---\ndoc_id: index-1\n---\n重复\n')
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(publisher),
                        'add', '.'], check=True, env=COMMIT_ENV)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(publisher),
                        'commit', '-qm', 'dup'], check=True, env=COMMIT_ENV)
        remote = pathlib.Path(self.fx['remote'])
        subprocess.run(['git', '--git-dir=' + str(remote), 'fetch', '-q', str(publisher),
                        '+main:refs/heads/main'], check=True, env=COMMIT_ENV)
        result = self.register_and_collect()
        blocked = [c for c in result['commits_processed'] if 'blocked' in c]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]['blocked']['code'], 'git_doc_conflict')
        self.assertEqual(blocked[0]['blocked']['details']['doc_id'], 'index-1')
        cp = evidence.checkpoint(self.db, self.eid)
        self.assertEqual(cp['blocked_count'], 1)
        # 被阻断提交不产生任何文档版本。
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM git_doc_versions WHERE endpoint_id=? AND doc_id='index-1'",
            (self.eid,)), 1)
        codes = [b['code'] for b in evidence.blocked_commits(self.db, self.eid)]
        self.assertEqual(codes, ['git_doc_conflict'])

    def test_conflict_commit_then_fix_resumes_to_head(self):
        publisher = pathlib.Path(self.fx['publisher'])
        (publisher / 'manuals' / 'dup2.md').write_text('---\ndoc_id: index-1\n---\n重复\n')
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(publisher),
                        'add', '.'], check=True, env=COMMIT_ENV)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(publisher),
                        'commit', '-qm', 'dup'], check=True, env=COMMIT_ENV)
        remote = pathlib.Path(self.fx['remote'])
        subprocess.run(['git', '--git-dir=' + str(remote), 'fetch', '-q', str(publisher),
                        '+main:refs/heads/main'], check=True, env=COMMIT_ENV)
        self.register_and_collect()
        self.assertEqual(evidence.checkpoint(self.db, self.eid)['confirmed_count'], 5)
        # 修复冲突并追加提交。
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(publisher),
                        'rm', '-q', 'manuals/dup2.md'], check=True)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(publisher),
                        'commit', '-qm', 'fix'], check=True, env=COMMIT_ENV)
        subprocess.run(['git', '--git-dir=' + str(remote), 'fetch', '-q', str(publisher),
                        '+main:refs/heads/main'], check=True, env=COMMIT_ENV)
        engine.freeze(self.db, self.eid)
        self.assertEqual(engine.assess(self.db, self.eid)['state'], 'active')
        out = engine.collect(self.db, self.eid)
        self.assertTrue(out['reached_head'])
        cp = evidence.checkpoint(self.db, self.eid)
        self.assertEqual(cp['confirmed_count'], 6)
        self.assertEqual(cp['blocked_count'], 1)

    def test_rewritten_remote_parks_at_divergence_and_blocks_collect(self):
        self.register_and_collect()
        bare_update(self.fx['remote'], 'refs/heads/main', self.fx['rewritten'])
        engine.freeze(self.db, self.eid)
        relation = engine.assess(self.db, self.eid)
        self.assertTrue(relation['diverged'])
        self.assertFalse(relation['ancestor'])
        self.assertEqual(relation['merge_base'], self.fx['first'])
        report = evidence.divergence_report(self.db, self.eid)
        self.assertTrue(report['diverged'])
        self.assertFalse(report['tip_is_ancestor_of_head'])
        with self.assertRaises(GateError) as caught:
            engine.collect(self.db, self.eid)
        self.assertEqual(caught.exception.code, 'git_diverged')

    def test_explicit_generation_reset_preserves_old_evidence(self):
        self.register_and_collect()
        old_versions = self.db.scalar(
            'SELECT COUNT(*) FROM git_doc_versions WHERE endpoint_id=? AND generation=1',
            (self.eid,))
        bare_update(self.fx['remote'], 'refs/heads/main', self.fx['rewritten'])
        engine.freeze(self.db, self.eid)
        engine.assess(self.db, self.eid)
        # 没有操作员理由不得切换世代。
        with self.assertRaises(GateError) as caught:
            engine.reset_generation(self.db, self.eid, '   ')
        self.assertEqual(caught.exception.code, 'git_reset_reason')
        engine.reset_generation(self.db, self.eid, '操作员确认采用改写后世代')
        gens = self.db.all(
            'SELECT generation,state FROM git_generations ORDER BY generation')
        self.assertEqual([(g['generation'], g['state']) for g in gens],
                         [(1, 'superseded'), (2, 'complete')])
        # 旧世代证据保留，新世代独立解释。
        self.assertEqual(self.db.scalar(
            'SELECT COUNT(*) FROM git_doc_versions WHERE endpoint_id=? AND generation=1',
            (self.eid,)), old_versions)
        cp = evidence.checkpoint(self.db, self.eid)
        self.assertEqual(cp['generation'], 2)
        self.assertEqual(cp['confirmed_tip'], self.fx['rewritten'])

    def test_reset_allowed_only_at_divergence(self):
        self.register_and_collect()
        with self.assertRaises(GateError) as caught:
            engine.reset_generation(self.db, self.eid, 'not diverged')
        self.assertEqual(caught.exception.code, 'git_not_diverged')

    def test_existing_review_and_release_tables_untouched(self):
        self.register_and_collect()
        # Git 采集不向既有 documents/intake/batches 流水线写入。
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM documents'), 0)
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM batches'), 0)
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM review_events'), 0)
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM releases'), 0)

    def test_rejects_branch_and_path_scope_escape(self):
        for bad_branch in ['../x', 'main~1', 'main:lock', '-x']:
            with self.assertRaises(GateError) as caught:
                engine.register(self.db, 'b' + str(abs(hash(bad_branch))), self.fx['remote'],
                                bad_branch, 'manuals', 'manuals', 'default', self.mirror_dir)
            self.assertEqual(caught.exception.code, 'git_branch')
        for bad_scope in ['../etc', '/absolute', 'a/../../b', '']:
            with self.assertRaises(GateError) as caught:
                engine.register(self.db, 'p' + str(abs(hash(bad_scope))), self.fx['remote'],
                                'main', bad_scope, 'manuals', 'default', self.mirror_dir)
            self.assertEqual(caught.exception.code, 'git_path_scope')

    def test_recursive_path_scope_and_empty_subtree(self):
        publisher = pathlib.Path(self.tmp / 'scoped')
        publisher.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(publisher)], check=True)
        (publisher / 'docs' / 'sub').mkdir(parents=True)
        (publisher / 'docs' / 'top.md').write_text('---\ndoc_id: top\n---\n')
        (publisher / 'docs' / 'sub' / 'nested.md').write_text('---\ndoc_id: nested\n---\n')
        (publisher / 'other.md').write_text('---\ndoc_id: outside\n---\n')
        subprocess.run(['git', '-C', str(publisher), 'add', '.'], check=True, env=COMMIT_ENV)
        subprocess.run(['git', '-C', str(publisher), 'commit', '-qm', 'init'],
                       check=True, env=COMMIT_ENV)
        remote = self.tmp / 'scoped.git'
        subprocess.run(['git', 'clone', '-q', '--bare', str(publisher), str(remote)],
                       check=True, env=COMMIT_ENV)
        engine.register(self.db, 'scoped', str(remote), 'main', 'docs',
                        'manuals', 'default', str(self.tmp / 'sm'))
        engine.freeze(self.db, 'scoped')
        engine.collect(self.db, 'scoped')
        docs = sorted(d['doc_id'] for d in evidence.commit_map(self.db, 'scoped')['documents'])
        self.assertEqual(docs, ['nested', 'top'])
        # 不存在的子树不报错，得到空可见集合。
        engine.register(self.db, 'empty', str(remote), 'main', 'docs2',
                        'manuals', 'default', str(self.tmp / 'em'))
        engine.freeze(self.db, 'empty')
        out = engine.collect(self.db, 'empty')
        self.assertTrue(out['reached_head'])
        self.assertEqual(evidence.commit_map(self.db, 'empty')['documents'], [])

    def test_operations_registry_exposes_git_source(self):
        ops = self.app.operations()
        for name in ('git.register', 'git.freeze', 'git.collect', 'git.assess',
                     'git.reset_generation', 'git.checkpoint', 'git.commit_map',
                     'git.document_timeline', 'git.verify_evidence', 'git.divergence',
                     'git.blocked'):
            self.assertIn(name, ops)
        result = self.app.dispatch('git.register', {
            'endpoint_id': 'via-dispatch', 'remote_url': self.fx['remote'], 'branch': 'main',
            'path_scope': 'manuals', 'source_id': 'manuals', 'profile_id': 'default',
            'mirror_dir': self.mirror_dir})
        self.assertEqual(result['endpoint_id'], 'via-dispatch')
        self.app.dispatch('git.freeze', {'endpoint_id': 'via-dispatch'})
        collected = self.app.dispatch('git.collect', {'endpoint_id': 'via-dispatch'})
        self.assertTrue(collected['reached_head'])


if __name__ == '__main__':
    unittest.main()
