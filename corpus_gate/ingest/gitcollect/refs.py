'''裸镜像维护与远端引用冻结。

采集器在本地维护一个 *裸* 镜像（无工作区、不签出内容，因此不会触发 smudge
过滤器或钩子），再把远端分支引用冻结到 ``refs/gate/<endpoint>/frozen``，
后续所有历史解释都基于冻结 OID，远端随后变化也不影响本轮采集。
'''
import re

from corpus_gate.errors import fail, identifier

# 分支名约束：禁止相对路径、反斜杠、特殊修饰符等，避免注入到 ref 参数。
_BRANCH_RE = re.compile(r'^(?!/|.*//|.*\.\.)[A-Za-z0-9._/-]{1,120}(?<!/)(?<!\.lock)$')
# 路径范围约束：限定在仓库内的相对目录，禁止绝对路径与逃逸。
_PATH_RE = re.compile(r'^[A-Za-z0-9._/-]{1,200}$')


def validate_branch(branch):
    if not isinstance(branch, str) or not _BRANCH_RE.fullmatch(branch) or branch.startswith('-') \
            or '..' in branch or '~' in branch or '^' in branch or ':' in branch \
            or '@{' in branch or '\\' in branch:
        fail('git_branch', 'Branch name is not an allowed simple ref', branch=str(branch)[:120])
    return branch


def validate_path_scope(scope):
    if not isinstance(scope, str) or scope in ('', '.', '/'):
        fail('git_path_scope', 'Path scope must be a bounded repository-relative directory')
    if scope.startswith(('/', '\\')) or '..' in scope.split('/'):
        fail('git_path_scope', 'Path scope escapes the allowed directory', scope=scope[:120])
    cleaned = scope.strip('/')
    if cleaned.startswith('-') or '\\' in cleaned or not _PATH_RE.fullmatch(cleaned) \
            or cleaned in ('.', '..'):
        fail('git_path_scope', 'Path scope escapes the allowed directory', scope=scope[:120])
    return cleaned


def frozen_ref(endpoint_id):
    identifier(endpoint_id)
    return f'refs/gate/{endpoint_id}/frozen'


def sync(endpoint):
    '''确保本地裸镜像存在并冻结远端分支，返回冻结状态。'''
    from corpus_gate.ingest.gitcollect import mirror_ops
    return mirror_ops.synchronize(endpoint)


def read_frozen(client, ref):
    result = client.run('rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}', check=False)
    if result.code != 0:
        fail('git_frozen_missing', 'Frozen remote ref does not resolve to a commit', ref=ref)
    return result.stdout.strip()


def describe_commit(client, oid):
    # 以 NUL 分隔字段，避免提交信息里的换行干扰。
    fmt = '%H%x00%T%x00%P%x00%ct%x00%s'
    line = client.text('show', '-s', '--format=' + fmt, oid)
    parts = line.split('\x00')
    if len(parts) != 5:
        fail('git_commit', 'Commit metadata has an unexpected shape', oid=oid)
    parents = parts[2].split() if parts[2] else []
    return {'commit': parts[0], 'tree': parts[1], 'parents': parents,
            'committed_at': int(parts[3]), 'subject': parts[4]}


def first_parent_chain(client, head_oid):
    '''从 head 沿第一父提交回溯，顺序为 head -> root。'''
    output = client.text('rev-list', '--first-parent', head_oid)
    return output.splitlines()


def merge_base(client, left, right):
    result = client.run('merge-base', left, right, check=False)
    if result.code != 0:
        return None
    return result.stdout.strip()
