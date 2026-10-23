import os
from pathlib import Path
import sys

os.environ['OPENAI_API_KEY'] = Path('/run/secrets/omlx-key').read_text().strip()
sys.path.insert(0, '/adapter')

import uvicorn

uvicorn.run('src.main:app', host='0.0.0.0', port=11436, log_level='warning')
