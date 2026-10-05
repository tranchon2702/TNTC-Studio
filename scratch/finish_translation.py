import sys, os
sys.path.insert(0, '.')
from scratch.translate_all import translate_python_file, translate_css_file

files = [
    r'webui/Main.py',
    r'app/utils/utils.py',
    r'webui/styles.css',
    r'app/utils/logging_utils.py',
    r'test/services/test_bgm.py',
    r'test/services/test_asgi_cors.py',
    r'test/services/test_api_authentication.py',
    r'docs/skill/mpt_agent.py',
    r'test/services/test_asgi_static_files.py',
    r'app/utils/file_security.py',
    r'test/services/test_cache_manager.py',
    r'app/models/schema.py',
    r'app/services/utils/video_effects.py',
    r'test/test_main.py',
]

for f in files:
    if f.endswith('.py'):
        translate_python_file(f)
    elif f.endswith('.css'):
        translate_css_file(f)
print("Finished targeted translation!")
