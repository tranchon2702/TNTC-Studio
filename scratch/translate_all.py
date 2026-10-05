import sys, os, re, json, time, io, tokenize, ast, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding='utf-8')

CACHE_FILE = 'scratch/translation_cache.json'
chinese_pattern = re.compile(r'[\u4e00-\u9fff]')

if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    except Exception:
        cache = {}
else:
    cache = {}

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
    text_clean = text.strip()
    if not text_clean or not chinese_pattern.search(text_clean):
        return text
    if text_clean in cache:
        return cache[text_clean]
    
    url = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=zh-CN&tl=vi&dt=t&q=' + urllib.parse.quote(text_clean)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                translated = ''.join(x[0] for x in data[0] if x[0])
                translated = refine_vietnamese(translated)
                cache[text_clean] = translated
                return translated
        except Exception as e:
            time.sleep(0.5 + attempt * 0.5)
    return text

def prefetch_all(texts):
    needed = list(set([t.strip() for t in texts if t.strip() and chinese_pattern.search(t.strip()) and t.strip() not in cache]))
    if not needed:
        return
    print(f"Translating {len(needed)} texts in parallel...")
    batch_size = 50
    for i in range(0, len(needed), batch_size):
        batch = needed[i:i+batch_size]
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(translate_api, batch))
        save_cache()
        print(f"  Processed {min(i+batch_size, len(needed))}/{len(needed)}...")
    save_cache()

def translate_python_file(path: str):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if not chinese_pattern.search(content):
        return
    
    lines = content.splitlines(keepends=True)
    
    # 1. AST for docstrings
    try:
        tree = ast.parse(content, filename=path)
    except Exception as e:
        print(f"Cannot parse AST for {path}: {e}")
        return

    docstring_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                val = node.body[0].value.value
                if chinese_pattern.search(val):
                    doc_node = node.body[0].value
                    for lno in range(doc_node.lineno, doc_node.end_lineno + 1):
                        docstring_lines.add(lno)

    # 2. Tokenize for comments
    comment_tokens = []
    try:
        tokens = list(tokenize.tokenize(io.BytesIO(content.encode('utf-8')).readline))
        for tok in tokens:
            if tok.type == tokenize.COMMENT and chinese_pattern.search(tok.string):
                comment_tokens.append(tok)
    except Exception as e:
        print(f"Tokenize error in {path}: {e}")
    
    # Collect all strings to translate
    to_translate = []
    for tok in comment_tokens:
        m = re.match(r'^(#+\s*)(.*)$', tok.string)
        if m and chinese_pattern.search(m.group(2)):
            to_translate.append(m.group(2))
    
    for lno in docstring_lines:
        orig_line = lines[lno - 1]
        s = orig_line.strip()
        for q in ['"""', "'''", '"', "'"]:
            if s.startswith(q):
                s = s[len(q):]
            if s.endswith(q):
                s = s[:-len(q)]
        if chinese_pattern.search(s):
            to_translate.append(s)
            
    prefetch_all(to_translate)
    
    # Replace comments
    new_lines = list(lines)
    for tok in comment_tokens:
        lno = tok.start[0]
        col = tok.start[1]
        orig_line = new_lines[lno - 1]
        m = re.match(r'^(#+\s*)(.*)$', tok.string)
        if m:
            prefix = m.group(1)
            body = m.group(2)
            trans_body = translate_api(body)
            code_before = orig_line[:col]
            new_lines[lno - 1] = code_before + prefix + trans_body + '\n'
            
    # Replace docstrings
    for lno in docstring_lines:
        orig_line = new_lines[lno - 1]
        if not chinese_pattern.search(orig_line):
            continue
        indent_match = re.match(r'^(\s*)', orig_line)
        indent = indent_match.group(1) if indent_match else ''
        s = orig_line.strip()
        
        m_single = re.match(r'^(r?["\']{3})(.*)(["\']{3})$', s)
        if m_single:
            q_start = m_single.group(1)
            body = m_single.group(2)
            q_end = m_single.group(3)
            trans_body = translate_api(body)
            new_lines[lno - 1] = f"{indent}{q_start}{trans_body}{q_end}\n"
            continue
            
        m_start = re.match(r'^(r?["\']{3})(.*)$', s)
        if m_start and m_start.group(2).strip():
            q_start = m_start.group(1)
            body = m_start.group(2)
            trans_body = translate_api(body)
            new_lines[lno - 1] = f"{indent}{q_start}{trans_body}\n"
            continue
            
        m_end = re.match(r'^(.*)(["\']{3})$', s)
        if m_end and m_end.group(1).strip():
            body = m_end.group(1)
            q_end = m_end.group(2)
            trans_body = translate_api(body)
            new_lines[lno - 1] = f"{indent}{trans_body}{q_end}\n"
            continue
            
        if chinese_pattern.search(s):
            trans_body = translate_api(s)
            new_lines[lno - 1] = f"{indent}{trans_body}\n"

    new_content = ''.join(new_lines)
    # Check syntax before saving
    try:
        ast.parse(new_content, filename=path)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"[OK] Python: {path}")
    except Exception as e:
        print(f"[FAIL] AST verification error in {path}: {e}")

def translate_css_file(path: str):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    if not chinese_pattern.search(content):
        return
    
    # CSS comments: /* ... */
    comments = re.findall(r'/\*[\s\S]*?\*/', content)
    to_trans = []
    for c in comments:
        if chinese_pattern.search(c):
            lines = c.splitlines()
            for l in lines:
                s = l.strip().lstrip('/*').rstrip('*/').strip()
                if chinese_pattern.search(s):
                    to_trans.append(s)
    prefetch_all(to_trans)
    
    def repl_css(m):
        block = m.group(0)
        if not chinese_pattern.search(block):
            return block
        lines = block.splitlines(keepends=True)
        new_lines = []
        for l in lines:
            indent = re.match(r'^\s*', l).group(0)
            s = l.strip()
            if s.startswith('/*') and s.endswith('*/'):
                inner = s[2:-2].strip()
                if chinese_pattern.search(inner):
                    new_lines.append(f"{indent}/* {translate_api(inner)} */\n")
                else:
                    new_lines.append(l)
            elif s.startswith('/*'):
                inner = s[2:].strip()
                if chinese_pattern.search(inner):
                    new_lines.append(f"{indent}/* {translate_api(inner)}\n")
                else:
                    new_lines.append(l)
            elif s.endswith('*/'):
                inner = s[:-2].strip()
                if chinese_pattern.search(inner):
                    new_lines.append(f"{indent}{translate_api(inner)} */\n")
                else:
                    new_lines.append(l)
            else:
                if chinese_pattern.search(s):
                    new_lines.append(f"{indent}{translate_api(s)}\n")
                else:
                    new_lines.append(l)
        return ''.join(new_lines)
        
    new_content = re.sub(r'/\*[\s\S]*?\*/', repl_css, content)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print(f"[OK] CSS: {path}")

def translate_line_comment_file(path: str):
    # For .toml, .sh
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    to_trans = []
    for l in lines:
        s = l.strip()
        if s.startswith('#') and chinese_pattern.search(s):
            m = re.match(r'^#+\s*(.*)$', s)
            if m and chinese_pattern.search(m.group(1)):
                to_trans.append(m.group(1))
    
    prefetch_all(to_trans)
    
    new_lines = []
    for l in lines:
        s = l.strip()
        if s.startswith('#') and chinese_pattern.search(s):
            indent = re.match(r'^\s*', l).group(0)
            m = re.match(r'^(#+\s*)(.*)$', s)
            if m and chinese_pattern.search(m.group(2)):
                prefix = m.group(1)
                body = m.group(2)
                trans_body = translate_api(body)
                new_lines.append(f"{indent}{prefix}{trans_body}\n")
            else:
                new_lines.append(l)
        else:
            new_lines.append(l)
            
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print(f"[OK] Comment file: {path}")

def main():
    ignore_dirs = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', 'scratch'}
    # Find all relevant files
    files_to_process = []
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        for f in files:
            p = os.path.join(root, f)
            ext = os.path.splitext(f)[1]
            if ext == '.py':
                files_to_process.append(('py', p))
            elif ext == '.css' and 'styles.css' in f:
                files_to_process.append(('css', p))
            elif ext in ('.toml', '.sh') and f in ('config.example.toml', 'pyproject.toml', 'webui.sh'):
                files_to_process.append(('comment', p))
    
    print(f"Found {len(files_to_process)} candidate files to inspect.")
    
    for ftype, p in files_to_process:
        try:
            if ftype == 'py':
                translate_python_file(p)
            elif ftype == 'css':
                translate_css_file(p)
            elif ftype == 'comment':
                translate_line_comment_file(p)
        except Exception as e:
            print(f"Error processing {p}: {e}")
            
    save_cache()
    print("Done all files!")

if __name__ == '__main__':
    main()
