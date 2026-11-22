import sys
import os
import runpy

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)
os.chdir(_dir)

# Local Web/Electron debugging defaults. Environment variables supplied by a
# deployment still take precedence, so production can switch providers without
# editing application code.
os.environ.setdefault('PATHCOPILOT_LOCAL_MODE', '1')
os.environ.setdefault('PATHCOPILOT_RUNTIME_MODE', 'managed')
os.environ.setdefault('API_PROVIDER', 'openrouter')
os.environ.setdefault('PATHCOPILOT_API_URL', 'https://openrouter.ai/api/v1/chat/completions')
os.environ.setdefault('PATHCOPILOT_MODEL', 'google/gemini-3.1-pro-preview')

runpy.run_path(os.path.join(_dir, 'app.py'), run_name='__main__')
