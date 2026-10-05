import sys, os, re, json, urllib.request, urllib.parse, ast

sys.stdout.reconfigure(encoding='utf-8')

CACHE_FILE = 'scratch/translation_cache.json'
chinese_pattern = re.compile(r'[\u4e00-\u9fff]')

with open(CACHE_FILE, 'r', encoding='utf-8') as f:
    cache = json.load(f)

def translate(text):
    t_clean = text.strip()
    if not t_clean or not chinese_pattern.search(t_clean):
        return text
    if t_clean in cache and not chinese_pattern.search(cache[t_clean]):
        return cache[t_clean]
    url = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=zh-CN&tl=vi&dt=t&q=' + urllib.parse.quote(t_clean)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            res = ''.join(x[0] for x in data[0] if x[0])
            cache[t_clean] = res
            return res
    except Exception:
        return text

def process_css():
    with open('webui/styles.css', 'r', encoding='utf-8') as f:
        content = f.read()
    
    def repl_block(m):
        full = m.group(0)
        if not chinese_pattern.search(full):
            return full
        # inside /* and */
        inner = full[2:-2].strip()
        lines = inner.splitlines()
        trans_lines = []
        for l in lines:
            ls = l.strip()
            if chinese_pattern.search(ls):
                trans_lines.append(translate(ls))
            else:
                trans_lines.append(ls)
        joined = '\n   '.join(trans_lines)
        return f"/* {joined} */"
        
    new_content = re.sub(r'/\*[\s\S]*?\*/', repl_block, content)
    with open('webui/styles.css', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("styles.css updated")

def process_py_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    tree = ast.parse(''.join(lines), filename=path)
    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                d = node.body[0].value
                if chinese_pattern.search(d.value):
                    for lno in range(d.lineno, d.end_lineno + 1):
                        doc_lines.add(lno)
                        
    new_lines = []
    for idx, l in enumerate(lines, 1):
        if not chinese_pattern.search(l):
            new_lines.append(l)
            continue
            
        # Is it in docstring?
        if idx in doc_lines:
            indent = re.match(r'^\s*', l).group(0)
            s = l.strip()
            # check quotes
            q_start = ''
            q_end = ''
            for q in ['"""', "'''"]:
                if s.startswith(q):
                    q_start = q
                    s = s[len(q):]
                if s.endswith(q):
                    q_end = q
                    s = s[:-len(q)]
            if chinese_pattern.search(s):
                s = translate(s)
            new_lines.append(f"{indent}{q_start}{s}{q_end}\n")
            continue
            
        # Check if line has #
        if '#' in l:
            # check where # is
            hash_idx = l.index('#')
            code_part = l[:hash_idx]
            comment_part = l[hash_idx:]
            m = re.match(r'^(#+\s*)(.*)$', comment_part)
            if m and chinese_pattern.search(m.group(2)):
                prefix = m.group(1)
                body = m.group(2).strip()
                t_body = translate(body)
                new_lines.append(f"{code_part}{prefix}{t_body}\n")
            else:
                new_lines.append(l)
        else:
            new_lines.append(l)
            
    new_content = ''.join(new_lines)
    try:
        ast.parse(new_content, filename=path)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"[OK] {path}")
    except Exception as e:
        print(f"[FAIL] {path}: {e}")

if __name__ == '__main__':
    process_css()
    # Check all remaining files
    remaining = [
        r'webui/Main.py',
        r'app/utils/utils.py',
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
    for p in remaining:
        process_py_file(p)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
