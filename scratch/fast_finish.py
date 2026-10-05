import sys, os, re, json, time, ast, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding='utf-8')

CACHE_FILE = 'scratch/translation_cache.json'
chinese_pattern = re.compile(r'[\u4e00-\u9fff]')

with open(CACHE_FILE, 'r', encoding='utf-8') as f:
    cache = json.load(f)

def save_cache():
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def refine_vietnamese(text: str) -> str:
    phrase_replacements = [
        ("phát hành tài nguyên", "giải phóng tài nguyên"),
        ("Phát hành tài nguyên", "Giải phóng tài nguyên"),
        ("phát hành bộ nhớ", "giải phóng bộ nhớ"),
        ("Phát hành bộ nhớ", "Giải phóng bộ nhớ"),
        ("người quản lý tác vụ", "trình quản lý tác vụ"),
        ("Người quản lý tác vụ", "Trình quản lý tác vụ"),
        ("mô hình lớn", "mô hình ngôn ngữ lớn (LLM)"),
        ("Mô hình lớn", "Mô hình ngôn ngữ lớn (LLM)"),
        ("hoạt ảnh bị trả lại", "hiệu ứng nảy (spring)"),
        ("hoạt ảnh nảy", "hiệu ứng nảy"),
        ("chia tỷ lệ", "thu phóng"),
        ("độ mờ", "độ mờ đục"),
        ("ánh sáng trước", "kiểm tra trước (preflight)"),
        ("Ánh sáng trước", "Kiểm tra trước (preflight)"),
        ("con trỏ tập tin", "con trỏ tệp (file pointer)"),
        ("rơi xuống đĩa", "ghi vào đĩa"),
        ("rơi đĩa", "ghi đĩa"),
    ]
    for old, new in phrase_replacements:
        text = text.replace(old, new)
    return text

def translate_api(text: str) -> str:
    t_clean = text.strip()
    if not t_clean or not chinese_pattern.search(t_clean):
        return text
    if t_clean in cache and not chinese_pattern.search(cache[t_clean]):
        return cache[t_clean]
    url = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=zh-CN&tl=vi&dt=t&q=' + urllib.parse.quote(t_clean)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                res = ''.join(x[0] for x in data[0] if x[0])
                res = refine_vietnamese(res)
                cache[t_clean] = res
                return res
        except Exception:
            time.sleep(0.5)
    return text

def prefetch_all(texts):
    needed = list(set([t.strip() for t in texts if t.strip() and chinese_pattern.search(t.strip()) and (t.strip() not in cache or chinese_pattern.search(cache[t.strip()]))]))
    if not needed:
        return
    print(f"Prefetching {len(needed)} texts...", flush=True)
    batch_size = 30
    for i in range(0, len(needed), batch_size):
        batch = needed[i:i+batch_size]
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(translate_api, batch))
        save_cache()
    print("Prefetch complete!", flush=True)

def process_css():
    with open('webui/styles.css', 'r', encoding='utf-8') as f:
        content = f.read()
    
    matches = re.findall(r'/\*[\s\S]*?\*/', content)
    to_trans = []
    for m in matches:
        if chinese_pattern.search(m):
            inner = m[2:-2].strip()
            for line in inner.splitlines():
                if chinese_pattern.search(line.strip()):
                    to_trans.append(line.strip())
    prefetch_all(to_trans)
    
    def repl(m):
        full = m.group(0)
        if not chinese_pattern.search(full):
            return full
        inner = full[2:-2].strip()
        lines = inner.splitlines()
        trans_lines = []
        for l in lines:
            ls = l.strip()
            if chinese_pattern.search(ls):
                trans_lines.append(translate_api(ls))
            else:
                trans_lines.append(ls)
        joined = '\n   '.join(trans_lines)
        return f"/* {joined} */"
        
    new_content = re.sub(r'/\*[\s\S]*?\*/', repl, content)
    with open('webui/styles.css', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("styles.css updated successfully", flush=True)

def process_py_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    if not chinese_pattern.search(content):
        return
        
    lines = content.splitlines(keepends=True)
    try:
        tree = ast.parse(content, filename=path)
    except Exception as e:
        print(f"AST parse error in {path}: {e}", flush=True)
        return
        
    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                d = node.body[0].value
                if chinese_pattern.search(d.value):
                    for lno in range(d.lineno, d.end_lineno + 1):
                        doc_lines.add(lno)
                        
    to_trans = []
    for idx, l in enumerate(lines, 1):
        if not chinese_pattern.search(l):
            continue
        if idx in doc_lines:
            s = l.strip()
            for q in ['"""', "'''"]:
                if s.startswith(q):
                    s = s[len(q):]
                if s.endswith(q):
                    s = s[:-len(q)]
            if chinese_pattern.search(s):
                to_trans.append(s)
        elif '#' in l:
            hash_idx = l.index('#')
            comment_part = l[hash_idx:]
            m = re.match(r'^(#+\s*)(.*)$', comment_part)
            if m and chinese_pattern.search(m.group(2)):
                to_trans.append(m.group(2).strip())
                
    prefetch_all(to_trans)
    
    new_lines = []
    for idx, l in enumerate(lines, 1):
        if not chinese_pattern.search(l):
            new_lines.append(l)
            continue
            
        line_ending = '\r\n' if l.endswith('\r\n') else '\n'
        l_stripped = l.rstrip('\r\n')
        
        if idx in doc_lines:
            indent = re.match(r'^\s*', l_stripped).group(0)
            s = l_stripped.strip()
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
                s = translate_api(s)
            new_lines.append(f"{indent}{q_start}{s}{q_end}{line_ending}")
            continue
            
        if '#' in l_stripped:
            hash_idx = l_stripped.index('#')
            code_part = l_stripped[:hash_idx]
            comment_part = l_stripped[hash_idx:]
            m = re.match(r'^(#+\s*)(.*)$', comment_part)
            if m and chinese_pattern.search(m.group(2)):
                prefix = m.group(1)
                body = m.group(2).strip()
                t_body = translate_api(body)
                new_lines.append(f"{code_part}{prefix}{t_body}{line_ending}")
            else:
                new_lines.append(l)
        else:
            new_lines.append(l)
            
    new_content = ''.join(new_lines)
    try:
        ast.parse(new_content, filename=path)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"[OK] {path}", flush=True)
    except Exception as e:
        print(f"[FAIL] {path}: {e}", flush=True)

if __name__ == '__main__':
    process_css()
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
    save_cache()
    print("ALL DONE!", flush=True)
