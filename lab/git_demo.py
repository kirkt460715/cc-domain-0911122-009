'''只读 Git 来源采集的本地综合演练（不连接任何真实服务）。

用 lab.git_source 合成的裸远端，完整走一遍：
冻结远端引用 -> 沿第一父提交链采集 -> 展示提交到文档版本映射 ->
制造同树重复 doc_id 冲突 -> 演练远端改写后的分叉停放与操作员续世代。
运行：python3 -m lab.git_demo
'''
import json
import os
import pathlib
import subprocess
import tempfile

from corpus_gate.service import CorpusGate
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate.ingest.gitcollect import engine, evidence
from lab.git_source import build

AUTH = {'purposes': ['research'], 'regions': ['cn'],
        'valid_from': '2026-01-01T00:00:00Z', 'valid_until': '2027-01-01T00:00:00Z',
        'obligations': [], 'allow_export': True}
ENV = {**os.environ,
       'GIT_AUTHOR_NAME': '本地资料员', 'GIT_AUTHOR_EMAIL': 'manual@local.invalid',
       'GIT_COMMITTER_NAME': '本地资料员', 'GIT_COMMITTER_EMAIL': 'manual@local.invalid',
       'GIT_AUTHOR_DATE': '2026-11-02T00:00:00Z', 'GIT_COMMITTER_DATE': '2026-11-02T00:00:00Z'}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        fixture = build(str(tmp))
        remote = pathlib.Path(fixture['remote'])
        publisher = pathlib.Path(fixture['publisher'])
        mirror_dir = str(tmp / 'mirrors')
        app = CorpusGate()
        db = app.db
        sources.create(db, 'manuals', AUTH)
        profiles.put(db, 'default', 0, {})
        endpoint = 'manuals-git'

        engine.register(db, endpoint, fixture['remote'], 'main', 'manuals',
                        'manuals', 'default', mirror_dir)
        engine.freeze(db, endpoint)
        collected = engine.collect(db, endpoint)

        print('== 第一父提交链上的提交 -> 文档变化 ==')
        for commit in collected['commits_processed']:
            label = '合并提交' if commit['is_merge'] else '线性提交'
            print(f"#{commit['ordinal']} {commit['commit'][:10]} [{label}]")
            for change in commit['changes']:
                print('   ', json.dumps(change, ensure_ascii=False))

        print('\n== pump-1 的文档版本历史（身份为 doc_id，路径只是位置）==')
        timeline = evidence.document_timeline(db, endpoint, 'pump-1')
        for version in timeline['versions']:
            print(f"    v{version['version']} {version['path']} @{version['commit_oid'][:10]}")
        print('    最新可见状态:', [(s['status'], s['path']) for s in timeline['timeline']
              if s['status'] == 'withdrawn'])

        print('\n== 版本证据可由裸镜像独立重算 ==')
        proof = evidence.verify_blob_evidence(db, endpoint, 'pump-1', 1)
        print('    ', proof['blob_oid'][:10], 'matches =', proof['matches'])

        print('\n== 制造同树重复 doc_id 冲突 ==')
        (publisher / 'manuals' / 'dup2.md').write_text('---\ndoc_id: index-1\n---\n重复身份\n')
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(publisher),
                        'add', '.'], check=True, env=ENV)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(publisher),
                        'commit', '-qm', '重复文档身份'], check=True, env=ENV)
        subprocess.run(['git', '--git-dir=' + str(remote), 'fetch', '-q', str(publisher),
                        '+main:refs/heads/main'], check=True, env=ENV)
        engine.freeze(db, endpoint)
        engine.collect(db, endpoint)
        print('    blocked:', json.dumps(evidence.blocked_commits(db, endpoint),
                                         ensure_ascii=False))

        print('\n== 远端回退/改写后的分叉停放 ==')
        subprocess.run(['git', '--git-dir=' + str(remote), 'update-ref',
                        'refs/heads/main', fixture['rewritten']], check=True)
        engine.freeze(db, endpoint)
        relation = engine.assess(db, endpoint)
        print('    diverged =', relation['diverged'], 'merge_base =',
              relation['merge_base'][:10])
        print('    两边关系:', json.dumps(evidence.divergence_report(db, endpoint),
                                          ensure_ascii=False))

        print('\n== 操作员显式选择新采集世代（不自动清库，旧证据保留）==')
        kept = db.scalar(
            'SELECT COUNT(*) FROM git_doc_versions WHERE endpoint_id=? AND generation=1',
            (endpoint,))
        engine.reset_generation(db, endpoint, '演练：确认采用改写后的采集世代')
        checkpoint = evidence.checkpoint(db, endpoint)
        print('    旧世代保留版本数 =', kept)
        print('    新世代 =', checkpoint['generation'], 'state =',
              checkpoint['generation_state'], 'confirmed_tip =',
              checkpoint['confirmed_tip'][:10])
        app.close()


if __name__ == '__main__':
    main()
