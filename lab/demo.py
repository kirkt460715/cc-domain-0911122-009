import tempfile,json
from .git_source import build
with tempfile.TemporaryDirectory() as d:print(json.dumps(build(d),ensure_ascii=False,indent=2))
