import subprocess,os
from pathlib import Path
ENV={**os.environ,'GIT_AUTHOR_NAME':'本地资料员','GIT_AUTHOR_EMAIL':'manual@local.invalid','GIT_COMMITTER_NAME':'本地资料员','GIT_COMMITTER_EMAIL':'manual@local.invalid','GIT_AUTHOR_DATE':'2026-11-01T00:00:00Z','GIT_COMMITTER_DATE':'2026-11-01T00:00:00Z'}
def git(root,*args):return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-C',str(root),*args],env=ENV,stderr=subprocess.DEVNULL,text=True).strip()
def commit(root,message):git(root,'add','.');git(root,'commit','-qm',message);return git(root,'rev-parse','HEAD')
def build(directory):
    base=Path(directory);base.mkdir(parents=True,exist_ok=True);root=base/'publisher';remote=base/'manuals.git'
    root.mkdir();git(root,'init','-q','-b','main');(root/'manuals').mkdir()
    doc='---\ndoc_id: pump-1\n---\n# 泵组\n先断电。\n';(root/'manuals/pump.md').write_text(doc);first=commit(root,'登记维护说明')
    git(root,'mv','manuals/pump.md','manuals/pump-renamed.md');(root/'manuals/pump-renamed.md').write_text(doc+'检查压力。\n');moved=commit(root,'移动并更新步骤')
    git(root,'checkout','-qb','editorial');(root/'manuals/valve.md').write_text('---\ndoc_id: valve-1\n---\n# 阀门\n关闭后挂牌。\n');commit(root,'侧支新增阀门说明')
    git(root,'checkout','-q','main');(root/'manuals/index.md').write_text('---\ndoc_id: index-1\n---\n# 索引\n设备列表。\n');commit(root,'更新索引')
    git(root,'merge','-q','--no-ff','editorial','-m','合并资料');merged=git(root,'rev-parse','HEAD')
    git(root,'rm','-q','manuals/pump-renamed.md');deleted=commit(root,'撤下泵组说明')
    git(root,'checkout','-qb','conflict');(root/'manuals/duplicate.md').write_text('---\ndoc_id: valve-1\n---\n重复身份。\n');conflict=commit(root,'重复文档身份')
    git(root,'checkout','-q','main');git(root,'checkout','-qb','rewritten',first);(root/'manuals/pump.md').write_text(doc+'分叉版本。\n');rewritten=commit(root,'重新整理分支');git(root,'checkout','-q','main')
    subprocess.run(['git','clone','-q','--bare',str(root),str(remote)],check=True,env=ENV)
    return {'remote':str(remote),'publisher':str(root),'first':first,'moved':moved,'merged':merged,'deleted':deleted,'conflict':conflict,'rewritten':rewritten}
if __name__=='__main__':
    import argparse,json
    p=argparse.ArgumentParser();p.add_argument('directory');print(json.dumps(build(p.parse_args().directory),ensure_ascii=False,indent=2))
