'''提交到文档版本的映射、版本证据与可恢复位置查询。'''
import hashlib
import json

from corpus_gate.errors import fail
from corpus_gate.ingest.gitcollect import store
from corpus_gate.ingest.gitcollect.runner import GitClient
from corpus_gate.ingest.gitcollect import mirror_ops


def _client(db, endpoint):
    return GitClient(mirror_ops.mirror_path(endpoint['mirror_dir'], endpoint['endpoint_id']))


def verify_blob_evidence(db, endpoint_id, doc_id, version):
    '''用本地裸镜像重算 blob 内容，核对当前世代保存的版本证据。'''
    store.ensure_schema(db)
    endpoint = store.get_endpoint(db, endpoint_id)
    generation = store.active_generation(db, endpoint_id)
    row = db.one(
        'SELECT * FROM git_doc_versions WHERE endpoint_id=? AND generation=? '
        'AND doc_id=? AND version=?',
        (endpoint_id, generation, doc_id, version),
    )
    if row is None:
        fail('git_version_missing', 'Document version is not recorded')
    client = _client(db, endpoint)
    raw = client.bytes_run('cat-file', 'blob', row['blob_oid'])
    content_sha = hashlib.sha256(raw).hexdigest()
    blob_type = client.run('cat-file', '-t', row['blob_oid']).stdout.strip()
    return {
        'endpoint_id': endpoint_id, 'generation': generation, 'doc_id': doc_id,
        'version': version, 'commit_oid': row['commit_oid'], 'blob_oid': row['blob_oid'],
        'path': row['path'], 'blob_type': blob_type,
        'recorded_content_sha256': row['content_sha256'],
        'recomputed_content_sha256': content_sha,
        'matches': content_sha == row['content_sha256'] and blob_type == 'blob',
    }


def commit_map(db, endpoint_id, commit_oid=None):
    '''展示当前世代一个提交（默认最新确认提交）到文档版本/状态的完整映射。'''
    store.ensure_schema(db)
    endpoint = store.get_endpoint(db, endpoint_id)
    generation = store.active_generation(db, endpoint_id)
    if commit_oid is None:
        commit_oid = store.confirmed_tip(db, endpoint_id, generation)
        if commit_oid is None:
            fail('git_no_commit', 'No confirmed commit exists yet')
    commit = store.commit_row(db, endpoint_id, generation, commit_oid)
    if commit is None:
        fail('git_commit_missing', 'Commit is not part of the current generation')
    states = store.state_at(db, endpoint_id, generation, commit_oid)
    excluded = db.all(
        'SELECT path,mode,kind,reason FROM git_commit_excluded '
        'WHERE endpoint_id=? AND generation=? AND commit_oid=? ORDER BY path',
        (endpoint_id, generation, commit_oid),
    )
    return {
        'endpoint_id': endpoint_id, 'generation': generation, 'commit_oid': commit_oid,
        'tree_oid': commit['tree_oid'], 'parents': json.loads(commit['parent_oids']),
        'is_merge': bool(commit['is_merge']), 'ordinal': commit['ordinal'],
        'documents': states, 'excluded': excluded,
    }


def document_timeline(db, endpoint_id, doc_id):
    '''单个 doc_id 沿提交链的完整历史：版本、路径迁移与撤回。'''
    store.ensure_schema(db)
    store.get_endpoint(db, endpoint_id)
    generation = store.active_generation(db, endpoint_id)
    states = store.doc_history(db, endpoint_id, generation, doc_id)
    if not states:
        fail('git_doc_unknown', 'doc_id never appeared in the collected scope')
    versions = store.doc_versions(db, endpoint_id, generation, doc_id)
    return {'endpoint_id': endpoint_id, 'generation': generation, 'doc_id': doc_id,
            'versions': versions, 'timeline': states}


def checkpoint(db, endpoint_id):
    '''可恢复位置：当前世代最后一个已确认提交及其之前的确认范围。'''
    store.ensure_schema(db)
    endpoint = store.get_endpoint(db, endpoint_id)
    gen = store.current_generation(db, endpoint_id)
    generation = gen['generation'] if gen else None
    tip = store.confirmed_tip(db, endpoint_id, generation) if generation else None
    confirmed = db.all(
        'SELECT commit_oid,ordinal FROM git_commits WHERE endpoint_id=? AND generation=? '
        'AND state=? ORDER BY ordinal',
        (endpoint_id, generation, store.COMMIT_CONFIRMED),
    ) if generation else []
    blocked = db.all(
        'SELECT commit_oid,ordinal,code FROM git_blocked_commits '
        'WHERE endpoint_id=? AND generation=? ORDER BY ordinal',
        (endpoint_id, generation),
    ) if generation else []
    return {
        'endpoint_id': endpoint_id, 'state': endpoint['state'],
        'generation': generation or 0,
        'generation_state': gen['state'] if gen else None,
        'frozen_head': endpoint['head_oid'], 'confirmed_tip': tip,
        'confirmed_count': len(confirmed), 'blocked_count': len(blocked),
        'blocked': blocked,
        'resumable': tip is not None and gen is not None
        and gen['state'] in ('active', 'complete'),
        'confirmed_commits': confirmed,
    }


def divergence_report(db, endpoint_id):
    '''分叉待决时展示旧确认位置与新 HEAD 的两边关系。'''
    store.ensure_schema(db)
    endpoint = store.get_endpoint(db, endpoint_id)
    gen = store.current_generation(db, endpoint_id)
    generation = gen['generation'] if gen else None
    client = _client(db, endpoint)
    tip = store.confirmed_tip(db, endpoint_id, generation) if generation else None
    head = endpoint['head_oid']
    if tip is None:
        return {'endpoint_id': endpoint_id, 'generation': generation,
                'diverged': False, 'head_oid': head}
    base = client.run('merge-base', tip, head, check=False)
    base_oid = base.stdout.strip() if base.code == 0 else None
    ahead = client.lines('rev-list', '--count', f'{base_oid}..{tip}') if base_oid else []
    behind = client.lines('rev-list', '--count', f'{base_oid}..{head}') if base_oid else []
    return {
        'endpoint_id': endpoint_id, 'diverged': endpoint['state'] == store.GEN_DIVERGED,
        'generation': generation, 'generation_state': gen['state'],
        'confirmed_tip': tip, 'new_head': head, 'merge_base': base_oid,
        'old_lineage_commits': int(ahead[0]) if ahead else None,
        'new_lineage_commits': int(behind[0]) if behind else None,
        'tip_is_ancestor_of_head': client.run(
            'merge-base', '--is-ancestor', tip, head, check=False).code == 0,
    }


def blocked_commits(db, endpoint_id):
    '''当前世代因冲突等原因被跳过、未确认的提交及原因。'''
    store.ensure_schema(db)
    endpoint = store.get_endpoint(db, endpoint_id)
    generation = store.active_generation(db, endpoint_id)
    rows = db.all(
        'SELECT commit_oid,ordinal,code,detail FROM git_blocked_commits '
        'WHERE endpoint_id=? AND generation=? ORDER BY ordinal',
        (endpoint_id, generation),
    )
    for row in rows:
        row['detail'] = json.loads(row['detail'])
    return rows


def listing(db):
    store.ensure_schema(db)
    return [store.get_endpoint(db, row['endpoint_id'])
            for row in db.all('SELECT endpoint_id FROM git_endpoints ORDER BY endpoint_id')]
