'''Markdown front matter 解析与受限树对象读取。

front matter 按来源协议实现最小解析：UTF-8 文档首行 ``---`` 到下一个 ``---``
之间的单个 ``doc_id`` 键，值为 ASCII 标识，不引入完整 YAML 解释器，避免
非受信内容触发复杂解析行为。
'''
import re

from corpus_gate.errors import fail, identifier

_DOC_ID = re.compile(r'^doc_id\s*:\s*(\S+)\s*$')
_FENCE = '---'


def parse_front_matter(raw_bytes):
    '''返回 (doc_id, 正文字节)。不符合协议即拒绝，不做静默猜测。'''
    try:
        text = raw_bytes.decode('utf-8')
    except UnicodeDecodeError as exc:
        fail('git_encoding', 'Markdown document is not valid UTF-8')
    lines = text.split('\n')
    if not lines or lines[0] != _FENCE:
        fail('git_front_matter', 'Document must begin with a front matter fence')
    doc_id = None
    body_start = None
    for index in range(1, len(lines)):
        line = lines[index]
        if line == _FENCE:
            body_start = index + 1
            break
        if not line.strip():
            continue
        match = _DOC_ID.match(line)
        if match is None:
            fail('git_front_matter', 'Front matter contains an unsupported line', line=line[:200])
        if doc_id is not None:
            fail('git_front_matter', 'doc_id is declared more than once')
        doc_id = match.group(1)
    if body_start is None:
        fail('git_front_matter', 'Front matter fence is never closed')
    if doc_id is None:
        fail('git_front_matter', 'Front matter declares no doc_id')
    # identifier() 复用既有 ASCII 标识规则，非法值直接拒绝。
    identifier(doc_id, 'doc_id')
    body = '\n'.join(lines[body_start:])
    return doc_id, body


def list_tree(client, tree_oid, path_scope):
    '''递归列出某棵树在路径范围内的普通 .md blob。

    返回 [{path, oid, mode}]。符号链接（120000）、子模块（160000）、
    gitlink、可执行文件等非普通 blob 一律不采，并单独标记出来供审计。
    '''
    scope = path_scope.strip('/')
    prefix = '' if not scope else scope + '/'
    spec = f'{tree_oid}:{scope}' if scope else tree_oid
    if scope:
        # 受限子树在该提交中尚不存在：视为该范围可见文档为空。
        probe = client.run('cat-file', '-e', spec, check=False)
        if probe.code != 0:
            return [], []
    # -r 递归覆盖子目录；-z 以 NUL 分隔，路径可含空格或特殊字符；只取受限子树。
    output = client.bytes_run('ls-tree', '-r', '-z', spec)
    entries = [part for part in output.split(b'\x00') if part]
    documents, excluded = [], []
    for entry in entries:
        meta, name = entry.split(b'\t', 1)
        mode, kind, oid = meta.decode('ascii').split()
        path = prefix + name.decode('utf-8')
        if kind != 'blob':
            excluded.append({'path': path, 'mode': mode, 'kind': kind, 'reason': 'not_a_blob'})
            continue
        if mode != '100644':
            excluded.append({'path': path, 'mode': mode, 'kind': kind, 'reason': 'non_regular_mode'})
            continue
        if not path.endswith('.md'):
            excluded.append({'path': path, 'mode': mode, 'kind': kind, 'reason': 'not_markdown'})
            continue
        documents.append({'path': path, 'oid': oid, 'mode': mode})
    documents.sort(key=lambda item: item['path'])
    excluded.sort(key=lambda item: item['path'])
    return documents, excluded
