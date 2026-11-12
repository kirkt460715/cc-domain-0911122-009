'''只读、受限的 git 命令封装。

采集器只通过这里访问 git：固定参数列表（不经过 shell）、清理环境变量、
全局关闭 hooks、子模块与外部过滤器，保证远端内容无法借采集过程执行代码。
'''
import os
import subprocess

from corpus_gate.errors import fail, GateError


def _scrubbed_env():
    # 仅保留最小运行环境，剔除可能影响 git 行为或泄露凭据的变量。
    keep = ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'HOME')
    env = {key: value for key, value in os.environ.items() if key in keep}
    # 显式占位，避免 git 在缺失 HOME 时回退到可疑配置位置。
    env.setdefault('HOME', '/dev/null')
    # 不向任何远端发送身份信息。
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    return env


# 每个调用都强制的安全开关：
# - core.hooksPath=/dev/null  不执行任何钩子
# - protocol.*.allow 收紧协议，禁用 ext 协议
# - fetch 关闭子模块递归、关闭外部过滤器
_SAFE_CONFIG = (
    '-c', 'core.hooksPath=/dev/null',
    '-c', 'core.fsmonitor=false',
    '-c', 'core.sshCommand=false',
    '-c', 'core.gitProxy=',
    '-c', 'protocol.ext.allow=never',
    '-c', 'protocol.file.allow=user',
    '-c', 'filter.lfs.smudge=',
    '-c', 'filter.lfs.required=false',
    '-c', 'gc.auto=0',
    '-c', 'uploadpack.allowFilter=true',
)

# 采集器永远不允许调用的动词（写工作区/执行外部内容/子模块）。
_FORBIDDEN_VERBS = {
    'submodule', 'hook', 'grep', 'filter-branch', 'rebase', 'pull',
    'cherry-pick', 'revert', 'bisect', 'worktree', 'clean',
}


class GitResult:
    def __init__(self, code, stdout, stderr):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr


class GitClient:
    def __init__(self, git_dir):
        self.git_dir = str(git_dir)
        self.env = _scrubbed_env()

    def _command(self, verb, args):
        if verb in _FORBIDDEN_VERBS:
            fail('git_forbidden', 'Git verb is not permitted by the read-only collector', verb=verb)
        return ['git', *_SAFE_CONFIG, '--git-dir', self.git_dir, verb, *args]

    def run(self, verb, *args, check=True):
        command = self._command(verb, list(args))
        try:
            completed = subprocess.run(
                command,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                timeout=300,
            )
        except FileNotFoundError as exc:
            raise GateError('git_unavailable', 'git executable is not available') from exc
        except subprocess.TimeoutExpired as exc:
            raise GateError('git_timeout', 'git command timed out', verb=verb) from exc
        result = GitResult(completed.returncode, completed.stdout, completed.stderr)
        if check and completed.returncode != 0:
            raise GateError(
                'git_command',
                'git command failed',
                verb=verb,
                stderr=completed.stderr.strip()[:500],
            )
        return result

    def text(self, verb, *args):
        return self.run(verb, *args).stdout

    def lines(self, verb, *args):
        output = self.text(verb, *args)
        return output.splitlines() if output else []

    def bytes_run(self, verb, *args):
        command = self._command(verb, list(args))
        try:
            completed = subprocess.run(
                command,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=300,
            )
        except FileNotFoundError as exc:
            raise GateError('git_unavailable', 'git executable is not available') from exc
        except subprocess.TimeoutExpired as exc:
            raise GateError('git_timeout', 'git command timed out', verb=verb) from exc
        if completed.returncode != 0:
            raise GateError(
                'git_command', 'git command failed', verb=verb,
                stderr=completed.stderr.decode('utf-8', 'replace').strip()[:500],
            )
        return completed.stdout

    def object_exists(self, oid):
        return self.run('cat-file', '-e', oid, check=False).code == 0
