'''本地裸镜像的创建、增量同步与引用冻结的具体实现。

只使用 ``clone --mirror`` / ``fetch`` 与本地点引用，不签出工作区、不递归
子模块，不运行任何远端自带脚本。
'''
import pathlib
import subprocess

from corpus_gate.errors import fail
from corpus_gate.ingest.gitcollect.runner import GitClient, _scrubbed_env, _SAFE_CONFIG
from corpus_gate.ingest.gitcollect import refs as refmod


def _git_base():
    return ['git', *_SAFE_CONFIG]


def mirror_path(store_dir, endpoint_id):
    return pathlib.Path(store_dir) / f'{endpoint_id}.git'


def _run(command):
    try:
        completed = subprocess.run(
            command, env=_scrubbed_env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, shell=False, timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        fail('git_timeout', 'git network operation timed out')
    if completed.returncode != 0:
        fail('git_sync', 'Remote synchronization failed',
             stderr=completed.stderr.strip()[:500])
    return completed


def synchronize(endpoint):
    '''endpoint 为已登记配置 dict。返回冻结结果，不修改数据库。'''
    store = pathlib.Path(endpoint['mirror_dir'])
    store.mkdir(parents=True, exist_ok=True)
    target = mirror_path(store, endpoint['endpoint_id'])
    remote = endpoint['remote_url']
    ref = refmod.frozen_ref(endpoint['endpoint_id'])
    if not target.exists():
        # 镜像克隆：取全部引用，但不签出、不初始化子模块。
        command = _git_base() + [
            'clone', '--mirror', '--quiet', '--no-local',
            '--config', 'core.hooksPath=/dev/null',
            remote, str(target),
        ]
        _run(command)
    else:
        # 增量抓取：prune 已删引用，但不自动合并到本地分支。
        command = _git_base() + [
            '--git-dir', str(target),
            '-c', 'remote.origin.url=' + remote,
            'fetch', '--prune', '--quiet', '--no-recurse-submodules',
            'origin',
        ]
        _run(command)

    client = GitClient(target)
    branch = endpoint['branch']
    remote_ref = f'refs/heads/{branch}'
    resolved = client.run('rev-parse', '--verify', '--quiet', f'{remote_ref}^{{commit}}', check=False)
    if resolved.code != 0:
        fail('git_branch_missing', 'Authorized branch is not present on the remote', branch=branch)
    head_oid = resolved.stdout.strip()

    # 冻结：把远端分支 OID 固定到 gate 私有引用，之后只认这个引用。
    client.run('update-ref', ref, head_oid)
    frozen = refmod.read_frozen(client, ref)
    if frozen != head_oid:
        fail('git_freeze', 'Frozen ref does not match resolved remote head')
    return {'head_oid': head_oid, 'frozen_ref': ref, 'mirror_dir': str(target)}
