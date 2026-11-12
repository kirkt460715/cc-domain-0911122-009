'''Git 来源采集的持久化结构。

这些表独立于既有 intake/documents 流水线，专门保存提交到文档版本的映射、
版本证据、撤回事件与采集世代。既有审查结论（review_events）与已发布语料
（releases/release_members）维持原表与原规则，本模块不改动它们。
'''
from corpus_gate.errors import digest, fail

DDL = '''
CREATE TABLE IF NOT EXISTS git_endpoints(
  endpoint_id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL,
  state TEXT NOT NULL,
  remote_url TEXT NOT NULL,
  branch TEXT NOT NULL,
  path_scope TEXT NOT NULL,
  source_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  mirror_dir TEXT NOT NULL,
  frozen_ref TEXT NOT NULL,
  head_oid TEXT,
  generation INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS git_generations(
  endpoint_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  base_oid TEXT,
  head_oid TEXT NOT NULL,
  state TEXT NOT NULL,
  reason TEXT NOT NULL,
  digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(endpoint_id, generation)
);
CREATE TABLE IF NOT EXISTS git_commits(
  endpoint_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  commit_oid TEXT NOT NULL,
  tree_oid TEXT NOT NULL,
  parent_oids TEXT NOT NULL,
  first_parent TEXT,
  ordinal INTEGER NOT NULL,
  is_merge INTEGER NOT NULL,
  subject TEXT NOT NULL,
  committed_at INTEGER NOT NULL,
  state TEXT NOT NULL,
  digest TEXT NOT NULL,
  PRIMARY KEY(endpoint_id, generation, commit_oid)
);
CREATE TABLE IF NOT EXISTS git_doc_versions(
  endpoint_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  doc_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  commit_oid TEXT NOT NULL,
  blob_oid TEXT NOT NULL,
  path TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  body_sha256 TEXT NOT NULL,
  metadata TEXT NOT NULL,
  PRIMARY KEY(endpoint_id, generation, doc_id, version)
);
CREATE TABLE IF NOT EXISTS git_doc_states(
  endpoint_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  doc_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  commit_oid TEXT NOT NULL,
  status TEXT NOT NULL,
  path TEXT,
  blob_oid TEXT,
  PRIMARY KEY(endpoint_id, generation, doc_id, commit_oid)
);
CREATE TABLE IF NOT EXISTS git_commit_excluded(
  endpoint_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  commit_oid TEXT NOT NULL,
  path TEXT NOT NULL,
  mode TEXT NOT NULL,
  kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY(endpoint_id, generation, commit_oid, path)
);
CREATE TABLE IF NOT EXISTS git_blocked_commits(
  endpoint_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  commit_oid TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  code TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(endpoint_id, generation, commit_oid)
);
'''

# 采集状态机
GEN_ACTIVE = 'active'          # 沿提交链正常推进
GEN_DIVERGED = 'diverged'      # 远端回退/改写，停在分叉待决
GEN_SUPERSEDED = 'superseded'  # 操作员显式选择新世代后，旧世代归档
GEN_COMPLETE = 'complete'      # 已处理到冻结 HEAD

COMMIT_CONFIRMED = 'confirmed'
COMMIT_PENDING = 'pending'
COMMIT_BLOCKED = 'blocked'

# 单文档在某提交树中的可见状态
STATUS_VISIBLE = 'visible'
STATUS_WITHDRAWN = 'withdrawn'


def ensure_schema(db):
    # CREATE TABLE IF NOT EXISTS 幂等；须在事务外调用（executescript 会隐式提交）。
    if db.depth:
        fail('git_ddl_transaction', 'Initialize git collector schema outside a transaction')
    db.db.executescript(DDL)


def get_endpoint(db, endpoint_id):
    row = db.one('SELECT * FROM git_endpoints WHERE endpoint_id=?', (endpoint_id,))
    if row is None:
        fail('git_endpoint_missing', 'Git endpoint is not registered')
    return row


def current_generation(db, endpoint_id):
    row = db.one(
        'SELECT * FROM git_generations WHERE endpoint_id=? ORDER BY generation DESC LIMIT 1',
        (endpoint_id,),
    )
    return row


def active_generation(db, endpoint_id):
    gen = current_generation(db, endpoint_id)
    return gen['generation'] if gen else None


def confirmed_tip(db, endpoint_id, generation=None):
    '''当前世代已确认提交链的最远端（最新）提交 OID；空库返回 None。'''
    generation = active_generation(db, endpoint_id) if generation is None else generation
    row = db.one(
        "SELECT commit_oid FROM git_commits WHERE endpoint_id=? AND generation=? AND state=? "
        "ORDER BY ordinal DESC LIMIT 1",
        (endpoint_id, generation, COMMIT_CONFIRMED),
    )
    return row['commit_oid'] if row else None


def commit_row(db, endpoint_id, generation, commit_oid):
    return db.one(
        'SELECT * FROM git_commits WHERE endpoint_id=? AND generation=? AND commit_oid=?',
        (endpoint_id, generation, commit_oid),
    )


def state_at(db, endpoint_id, generation, commit_oid):
    rows = db.all(
        'SELECT doc_id,version,status,path,blob_oid FROM git_doc_states '
        'WHERE endpoint_id=? AND generation=? AND commit_oid=? ORDER BY doc_id',
        (endpoint_id, generation, commit_oid),
    )
    return rows


def head_states(db, endpoint_id, generation):
    '''最新确认提交上的每文档 *可见* 状态（撤回文档不作为对比基准）。'''
    tip = confirmed_tip(db, endpoint_id, generation)
    if tip is None:
        return []
    rows = state_at(db, endpoint_id, generation, tip)
    return [row for row in rows if row['status'] == STATUS_VISIBLE]


def doc_history(db, endpoint_id, generation, doc_id):
    return db.all(
        'SELECT s.* FROM git_doc_states s JOIN git_commits c '
        'ON c.endpoint_id=s.endpoint_id AND c.generation=s.generation AND c.commit_oid=s.commit_oid '
        'WHERE s.endpoint_id=? AND s.generation=? AND s.doc_id=? ORDER BY c.ordinal',
        (endpoint_id, generation, doc_id),
    )


def doc_versions(db, endpoint_id, generation, doc_id):
    return db.all(
        'SELECT * FROM git_doc_versions WHERE endpoint_id=? AND generation=? AND doc_id=? '
        'ORDER BY version',
        (endpoint_id, generation, doc_id),
    )


def next_version(db, endpoint_id, generation, doc_id):
    row = db.one(
        'SELECT MAX(version) AS v FROM git_doc_versions '
        'WHERE endpoint_id=? AND generation=? AND doc_id=?',
        (endpoint_id, generation, doc_id),
    )
    return (row['v'] or 0) + 1
