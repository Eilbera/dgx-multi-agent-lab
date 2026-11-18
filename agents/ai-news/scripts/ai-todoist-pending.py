import importlib.util
from pathlib import Path
p = Path(__file__).with_name('ai_todoist_sync.py')
spec = importlib.util.spec_from_file_location('ai_todoist_sync', p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
import json
print(json.dumps(m.prepare(), ensure_ascii=False, indent=2))
