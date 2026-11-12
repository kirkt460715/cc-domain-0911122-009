# corpus_gate 本地业务处理

本项目保存业务原始记录、版本及可核对的处理历史，命令入口通过 JSON 参数调用领域操作。现有业务模块位于 corpus_gate/，测试位于 tests/。数据文件与运行输出放在工作目录之外或 var/ 中。

## 运行环境

Python 3.11，标准库与 SQLite。项目不包含生产账号或真实用户数据。先执行 docker build -t corpus_gate-local .，之后 docker run --rm --network none corpus_gate-local；依赖在构建阶段准备，正式测试与演示不需要公网。

## 测试与语法检查

python3 -m unittest discover -s tests -v
python3 -m compileall -q corpus_gate lab tests

## 现有操作示例

python3 -m corpus_gate --db :memory: demo --fixture fixtures/demo.json
python3 -m unittest discover -s tests -v
python3 -m compileall -q corpus_gate
python3 -m corpus_gate --help
python3 -m corpus_gate --db corpus.sqlite3 operations
python3 -m corpus_gate --db corpus.sqlite3 check

## 本地来源

lab/ 提供独立的合作方协议或合成文档发生器，不连接生产服务。协议、样本含义和操作见 docs/来源协议.md。运行 python3 -m lab.demo 可查看来源行为，python3 -m lab.git_demo 演练只读 Git 来源采集（移动、删除、合并、重复 doc_id 冲突与远端改写后的分叉/世代切换），python3 -m lab.check 依次执行测试、语法检查、业务演示和来源演示。HTTP 服务默认绑定回环地址，测试使用自动分配端口并在结束时清理。

## 只读 Git 来源
corpus_gate/ingest/gitcollect/ 把获准裸仓库的固定分支与路径范围采集为带版本证据的文档历史：先冻结远端引用，再沿第一父提交链按 front matter 的 doc_id 建立身份，重命名归入同一文档历史、删除记为来源撤回，合并提交不重放侧支临时版本。提交级确认支持从最后确认点恢复；远端改写导致非祖先时停在分叉，由操作员显式选择新采集世代，不自动清库。采集全程禁用 hooks、子模块与仓库脚本，详见 docs/来源协议.md。CLI 入口为 git-collect，服务操作以 git. 为前缀（git.register/git.freeze/git.collect/git.assess/git.reset_generation/git.checkpoint/git.commit_map/git.document_timeline/git.verify_evidence/git.divergence/git.blocked）。
