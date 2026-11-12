'''只读 Git 来源采集引擎。

处理顺序（沿第一父提交链，root -> head）：
1. 远端引用先冻结，再解释冻结提交；
2. 每个提交读取该树在路径范围内的普通 .md blob，按 front matter 的 doc_id
   建立稳定身份；同树重复 doc_id 报冲突；
3. 与上一确认提交的可见状态对比：新增/内容变化形成新版本，纯重命名记路径
   迁移，删除形成明确撤回；合并提交保留父信息但不重放侧支临时版本；
4. 一个提交的全部文档处理结果在同一事务内落库后才置为 confirmed，因此
   中途失败可从最后一个 confirmed 提交继续。
'''
import hashlib

from corpus_gate.errors import fail, identifier, canonical, digest, GateError
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate.ingest.gitcollect import store, refs as refmod, mirror_ops
from corpus_gate.ingest.gitcollect.runner import GitClient
from corpus_gate.ingest.gitcollect.frontmatter import parse_front_matter, list_tree


# --------------------------------------------------------------------------- #
# 端点登记
# --------------------------------------------------------------------------- #
def register(db, endpoint_id, remote_url, branch, path_scope, source_id,
             profile_id, mirror_dir, expected_revision=0):
    store.ensure_schema(db)
    identifier(endpoint_id)
    refmod.validate_branch(branch)
    scope = refmod.validate_path_scope(path_scope)
    if not isinstance(remote_url, str) or not remote_url.strip() or len(remote_url) > 500:
        fail('git_remote', 'Remote URL must be bounded text')
    if '://' in remote_url and remote_url.split('://', 1)[0] not in ('file', 'https', 'http', 'ssh'):
        fail('git_remote_scheme', 'Remote URL scheme is not allowed')
    if not isinstance(mirror_dir, str) or not mirror_dir.strip():
        fail('git_mirror_dir', 'Mirror directory is required')
    # 来源与处理画像必须已存在且仍有效，采集沿用既有授权与文本规则。
    sources.get(db, source_id)
    profiles.get(db, profile_id)
    with db.transaction():
        existing = db.one('SELECT revision FROM git_endpoints WHERE endpoint_id=?', (endpoint_id,))
        if expected_revision != (existing['revision'] if existing else 0):
            fail('git_endpoint_stale', 'Endpoint revision changed')
        if existing:
            fail('git_endpoint_exists', 'Endpoint identifier is already registered')
        frozen = refmod.frozen_ref(endpoint_id)
        db.execute(
            "INSERT INTO git_endpoints(endpoint_id,revision,state,remote_url,branch,path_scope,"
            "source_id,profile_id,mirror_dir,frozen_ref,head_oid,generation,created_at) "
            "VALUES(?,1,'configured',?,?,?,?,?,?,?,NULL,0,?)",
            (endpoint_id, remote_url.strip(), branch, scope, source_id, profile_id,
             mirror_dir, frozen, db.clock()),
        )
        db.log('git.endpoint_registered', {'endpoint_id': endpoint_id, 'branch': branch,
                                           'path_scope': scope, 'source_id': source_id})
        return store.get_endpoint(db, endpoint_id)


def _client_for(db, endpoint):
    return GitClient(mirror_ops.mirror_path(endpoint['mirror_dir'], endpoint['endpoint_id']))


# --------------------------------------------------------------------------- #
# 冻结与分叉判定
# --------------------------------------------------------------------------- #
def freeze(db, endpoint_id):
    store.ensure_schema(db)
    endpoint = store.get_endpoint(db, endpoint_id)
    synced = mirror_ops.synchronize(endpoint)
    client = _client_for(db, endpoint)
    new_head = synced['head_oid']
    with db.transaction():
        db.execute('UPDATE git_endpoints SET head_oid=?,revision=revision+1 WHERE endpoint_id=?',
                   (new_head, endpoint_id))
        gen = store.current_generation(db, endpoint_id)
        if gen is None:
            db.execute(
                'INSERT INTO git_generations(endpoint_id,generation,base_oid,head_oid,state,'
                'reason,digest,created_at) VALUES(?,1,NULL,?,?,?,?,?)',
                (endpoint_id, new_head, store.GEN_ACTIVE, 'initial freeze',
                 digest([endpoint_id, 1, new_head]), db.clock()),
            )
        elif gen['state'] in (store.GEN_ACTIVE, store.GEN_COMPLETE, store.GEN_DIVERGED):
            # 仅维护当前待决/活动世代的冻结 HEAD；已归档世代保持原样。
            db.execute('UPDATE git_generations SET head_oid=? WHERE endpoint_id=? AND generation=?',
                       (new_head, endpoint_id, gen['generation']))
        db.log('git.ref_frozen', {'endpoint_id': endpoint_id, 'head_oid': new_head,
                                  'frozen_ref': synced['frozen_ref']})
    return assess(db, endpoint_id)


def assess(db, endpoint_id):
    '''判断当前冻结 HEAD 与已确认历史是否在同一条第一父链上。'''
    store.ensure_schema(db)
    endpoint = store.get_endpoint(db, endpoint_id)
    client = _client_for(db, endpoint)
    head = endpoint['head_oid']
    gen = store.current_generation(db, endpoint_id)
    tip = store.confirmed_tip(db, endpoint_id, gen['generation']) if gen else None
    relation = {'head_oid': head, 'confirmed_tip': tip, 'ancestor': None,
                'merge_base': None, 'diverged': False}
    if tip is None:
        return {**relation, 'state': gen['state'] if gen else store.GEN_ACTIVE}
    # tip 是否为新 HEAD 的祖先（同一链可继续推进）。
    ancestor = client.run('merge-base', '--is-ancestor', tip, head, check=False).code == 0
    base = client.run('merge-base', tip, head, check=False)
    relation['merge_base'] = base.stdout.strip() if base.code == 0 else None
    relation['ancestor'] = ancestor
    if ancestor:
        relation['diverged'] = False
        return {**relation, 'state': store.GEN_ACTIVE}
    # 远端回退或改写：旧位置已不是新 HEAD 的祖先，停在分叉待决。
    relation['diverged'] = True
    with db.transaction():
        db.execute('UPDATE git_generations SET state=? WHERE endpoint_id=? AND generation=?',
                   (store.GEN_DIVERGED, endpoint_id, gen['generation']))
        db.execute("UPDATE git_endpoints SET state=? WHERE endpoint_id=?",
                   (store.GEN_DIVERGED, endpoint_id))
        db.log('git.divergence_detected', {'endpoint_id': endpoint_id,
                                           'confirmed_tip': tip, 'head_oid': head,
                                           'merge_base': relation['merge_base']})
    return {**relation, 'state': store.GEN_DIVERGED}


def reset_generation(db, endpoint_id, reason):
    '''操作员显式选择新采集世代；旧世代归档，绝不自动清库。'''
    store.ensure_schema(db)
    endpoint = store.get_endpoint(db, endpoint_id)
    gen = store.current_generation(db, endpoint_id)
    if gen is None or gen['state'] != store.GEN_DIVERGED:
        fail('git_not_diverged', 'A new generation can only be chosen at a divergence')
    reason = reason if isinstance(reason, str) and reason.strip() else fail(
        'git_reset_reason', 'An explicit operator reason is required')
    with db.transaction():
        new_generation = gen['generation'] + 1
        db.execute('UPDATE git_generations SET state=? WHERE endpoint_id=? AND generation=?',
                   (store.GEN_SUPERSEDED, endpoint_id, gen['generation']))
        db.execute(
            'INSERT INTO git_generations(endpoint_id,generation,base_oid,head_oid,state,'
            'reason,digest,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (endpoint_id, new_generation, gen['head_oid'], endpoint['head_oid'],
             store.GEN_ACTIVE, reason.strip(),
             digest([endpoint_id, new_generation, endpoint['head_oid'], reason]), db.clock()),
        )
        # 旧世代的提交与版本证据保留；新世代从空确认位置重新解释冻结 HEAD。
        db.execute("UPDATE git_endpoints SET state='active',generation=?,revision=revision+1 "
                   'WHERE endpoint_id=?', (new_generation, endpoint_id))
        db.log('git.generation_reset', {'endpoint_id': endpoint_id,
                                        'old_generation': gen['generation'],
                                        'new_generation': new_generation, 'reason': reason.strip()})
    return collect(db, endpoint_id)


# --------------------------------------------------------------------------- #
# 单树可见状态
# --------------------------------------------------------------------------- #
def _visible_state(db, client, endpoint, commit):
    docs, excluded = list_tree(client, commit['tree'], endpoint['path_scope'])
    visible = {}
    for entry in docs:
        raw = client.bytes_run('cat-file', 'blob', entry['oid'])
        doc_id, body = parse_front_matter(raw)
        if doc_id in visible:
            fail('git_doc_conflict', 'Two documents in one tree share the same doc_id',
                 doc_id=doc_id, paths=sorted([visible[doc_id]['path'], entry['path']]),
                 commit=commit['commit'])
        content_sha = hashlib.sha256(raw).hexdigest()
        body_sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
        visible[doc_id] = {'path': entry['path'], 'blob_oid': entry['oid'],
                           'content_sha256': content_sha, 'body_sha256': body_sha}
    return visible, excluded


def _record_excluded(db, endpoint_id, generation, commit_oid, excluded):
    for item in excluded:
        db.execute(
            'INSERT OR REPLACE INTO git_commit_excluded VALUES(?,?,?,?,?,?,?)',
            (endpoint_id, generation, commit_oid, item['path'], item['mode'],
             item['kind'], item['reason']),
        )


def _record_blocked(db, endpoint_id, generation, meta, ordinal, exc):
    with db.transaction():
        db.execute(
            'INSERT OR REPLACE INTO git_blocked_commits VALUES(?,?,?,?,?,?,?)',
            (endpoint_id, generation, meta['commit'], ordinal, exc.code,
             canonical(exc.as_dict()), db.clock()),
        )
        db.log('git.commit_blocked', {'endpoint_id': endpoint_id, 'generation': generation,
                                      'commit': meta['commit'], 'ordinal': ordinal,
                                      'code': exc.code, 'details': exc.details})


# --------------------------------------------------------------------------- #
# 逐提交采集
# --------------------------------------------------------------------------- #
def collect(db, endpoint_id, max_commits=10000):
    store.ensure_schema(db)
    endpoint = store.get_endpoint(db, endpoint_id)
    if endpoint['state'] == store.GEN_DIVERGED:
        fail('git_diverged', 'Collector is parked at a divergence; operator must choose a generation',
             relation=assess(db, endpoint_id))
    client = _client_for(db, endpoint)
    frozen_head = refmod.read_frozen(client, endpoint['frozen_ref'])
    if endpoint['head_oid'] and endpoint['head_oid'] != frozen_head:
        fail('git_freeze_stale', 'Freeze again before collecting; endpoint head moved')
    chain = refmod.first_parent_chain(client, frozen_head)  # head -> root
    chain.reverse()                                        # root -> head
    if len(chain) > max_commits:
        fail('git_chain_too_large', 'Commit chain exceeds the processing limit', commits=len(chain))

    generation = store.current_generation(db, endpoint_id)['generation']
    tip = store.confirmed_tip(db, endpoint_id, generation)
    if tip is not None and tip not in chain:
        # 防御性检查：正常应在 assess 阶段就转入 diverged。
        return assess(db, endpoint_id)
    start_index = 0 if tip is None else chain.index(tip) + 1
    processed = []

    for ordinal, commit_oid in enumerate(chain):
        if ordinal < start_index:
            continue
        meta = refmod.describe_commit(client, commit_oid)
        try:
            result = _process_commit(db, client, endpoint, meta, ordinal, generation)
        except GateError as exc:
            # 冲突提交客观存在于历史：记录为 blocked 证据并跳过，使后续已修复
            # 提交仍可采集；它不会进入 confirmed 链，也不产生任何文档版本。
            if exc.code != 'git_doc_conflict':
                raise
            _record_blocked(db, endpoint_id, generation, meta, ordinal, exc)
            result = {'commit': commit_oid, 'ordinal': ordinal,
                      'is_merge': len(meta['parents']) > 1, 'parents': meta['parents'],
                      'blocked': exc.as_dict(), 'changes': [], 'excluded': []}
        processed.append(result)
    # 处理到冻结 HEAD 即把世代与端点标记为完整；否则保持 active 等待续采。
    reached_head = store.confirmed_tip(db, endpoint_id, generation) == frozen_head
    if reached_head:
        with db.transaction():
            db.execute('UPDATE git_generations SET state=? WHERE endpoint_id=? AND generation=?',
                       (store.GEN_COMPLETE, endpoint_id, generation))
            db.execute("UPDATE git_endpoints SET state='complete',revision=revision+1 "
                       'WHERE endpoint_id=?', (endpoint_id,))
    return {'endpoint_id': endpoint_id, 'generation': generation, 'frozen_head': frozen_head,
            'confirmed_tip': store.confirmed_tip(db, endpoint_id, generation),
            'commits_processed': processed,
            'chain_length': len(chain), 'reached_head': reached_head}


def _process_commit(db, client, endpoint, meta, ordinal, generation):
    endpoint_id = endpoint['endpoint_id']
    commit = {'commit': meta['commit'], 'tree': meta['tree']}
    visible, excluded = _visible_state(db, client, endpoint, commit)
    previous = {row['doc_id']: row
                for row in store.head_states(db, endpoint_id, generation)}

    # 整个提交在一个事务内：全部文档结果落库后才置 confirmed，保证可恢复边界。
    with db.transaction():
        changes = []
        all_ids = sorted(set(visible) | set(previous))
        for doc_id in all_ids:
            current = visible.get(doc_id)
            prior = previous.get(doc_id)
            if current is not None and prior is None:
                version = store.next_version(db, endpoint_id, generation, doc_id)
                _insert_version(db, endpoint, generation, doc_id, version, meta, current)
                _insert_state(db, endpoint, generation, doc_id, version, meta,
                              store.STATUS_VISIBLE, current)
                changes.append({'doc_id': doc_id, 'kind': 'added', 'version': version,
                                'path': current['path']})
            elif current is not None and prior is not None:
                content_changed = current['blob_oid'] != prior['blob_oid']
                renamed = current['path'] != prior['path']
                if content_changed:
                    version = store.next_version(db, endpoint_id, generation, doc_id)
                    _insert_version(db, endpoint, generation, doc_id, version, meta, current)
                    _insert_state(db, endpoint, generation, doc_id, version, meta,
                                  store.STATUS_VISIBLE, current)
                    changes.append({'doc_id': doc_id,
                                    'kind': 'renamed_modified' if renamed else 'modified',
                                    'version': version, 'path': current['path'],
                                    'previous_path': prior['path'] if renamed else None})
                else:
                    # 内容未变：延续同一版本，仅记录位置（纯重命名不产生新版本）。
                    _insert_state(db, endpoint, generation, doc_id, prior['version'], meta,
                                  store.STATUS_VISIBLE, current)
                    if renamed:
                        changes.append({'doc_id': doc_id, 'kind': 'renamed',
                                        'version': prior['version'], 'path': current['path'],
                                        'previous_path': prior['path']})
            elif current is None and prior is not None:
                # 明确的来源撤回事件：保留历史版本，标记 withdrawn，不再可见。
                _insert_state(db, endpoint, generation, doc_id, prior['version'], meta,
                              store.STATUS_WITHDRAWN, None)
                changes.append({'doc_id': doc_id, 'kind': 'withdrawn',
                                'version': prior['version'], 'path': prior['path']})

        _record_excluded(db, endpoint_id, generation, meta['commit'], excluded)
        is_merge = 1 if len(meta['parents']) > 1 else 0
        first_parent = meta['parents'][0] if meta['parents'] else None
        commit_digest = digest([meta['commit'], meta['tree'], sorted(visible),
                                endpoint['path_scope']])
        db.execute(
            'INSERT INTO git_commits VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (endpoint_id, generation, meta['commit'], meta['tree'],
             canonical(meta['parents']), first_parent, ordinal, is_merge,
             meta['subject'][:500], meta['committed_at'], store.COMMIT_CONFIRMED, commit_digest),
        )
        db.log('git.commit_confirmed', {'endpoint_id': endpoint_id, 'commit': meta['commit'],
                                        'generation': generation, 'ordinal': ordinal,
                                        'is_merge': bool(is_merge),
                                        'changes': changes, 'excluded': len(excluded)})
    return {'commit': meta['commit'], 'ordinal': ordinal, 'is_merge': len(meta['parents']) > 1,
            'parents': meta['parents'], 'changes': changes, 'excluded': excluded}


def _insert_version(db, endpoint, generation, doc_id, version, meta, current):
    metadata = canonical({'doc_id': doc_id, 'path': current['path'],
                          'commit': meta['commit'], 'blob_oid': current['blob_oid']})
    db.execute(
        'INSERT INTO git_doc_versions VALUES(?,?,?,?,?,?,?,?,?,?)',
        (endpoint['endpoint_id'], generation, doc_id, version, meta['commit'],
         current['blob_oid'], current['path'], current['content_sha256'],
         current['body_sha256'], metadata),
    )


def _insert_state(db, endpoint, generation, doc_id, version, meta, status, current):
    path = current['path'] if current else None
    blob = current['blob_oid'] if current else None
    db.execute(
        'INSERT INTO git_doc_states VALUES(?,?,?,?,?,?,?,?)',
        (endpoint['endpoint_id'], generation, doc_id, version, meta['commit'],
         status, path, blob),
    )
