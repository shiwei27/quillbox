import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import sys, os, traceback
sys.path.insert(0, ROOT)
os.chdir(ROOT)

steps = [
    ("yaml",         "import yaml"),
    ("fastapi",      "import fastapi, uvicorn"),
    ("db_manager",   "from core.db_manager import DBManager"),
    ("vector_store", "from core.vector_store import VectorStore"),
    ("ai_engine",    "from core.ai_engine import AIEngine"),
    ("email_sender", "from core.email_sender import queue_send"),
    ("server_cfg",   """
import yaml
cfg = yaml.safe_load(open('config.yaml', encoding='utf-8'))
vdb = cfg.get('vector_db', {})
from core.db_manager import DBManager
from core.vector_store import VectorStore
from core.ai_engine import AIEngine
db = DBManager(_os.path.join(ROOT, 'data', 'emails.db'))
print('DB ok')
vs = VectorStore(vdb.get('path','./data/chromadb'), vdb.get('embedding_model_path',''))
print('VS ok')
"""),
]

for name, code in steps:
    try:
        exec(code)
        print(f"OK: {name}")
    except Exception as e:
        print(f"FAIL: {name} -> {e}")
        traceback.print_exc()
        break

with open(_os.path.join(ROOT, 'import_test.log'), 'w', encoding='utf-8') as f:
    f.write("done")
