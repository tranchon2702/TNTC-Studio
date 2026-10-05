import sys, os, re, tokenize, ast
sys.stdout.reconfigure(encoding='utf-8')

chinese_pattern = re.compile(r'[\u4e00-\u9fff]')
ignore_dirs = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', 'scratch'}

stats = {}

for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ignore_dirs]
    for file in files:
        path = os.path.join(root, file)
        ext = os.path.splitext(file)[1]
        
        if ext == '.py':
            comments_count = 0
            docstrings_count = 0
            try:
                with open(path, 'rb') as f:
                    tokens = list(tokenize.tokenize(f.readline))
                for tok in tokens:
                    if tok.type == tokenize.COMMENT and chinese_pattern.search(tok.string):
                        comments_count += 1
            except Exception:
                pass
            
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    tree = ast.parse(f.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                        doc = ast.get_docstring(node, clean=False)
                        if doc and chinese_pattern.search(doc):
                            docstrings_count += 1
            except Exception:
                pass
            
            if comments_count or docstrings_count:
                stats[path] = {'type': 'py', 'comments': comments_count, 'docstrings': docstrings_count}
        
        elif ext in ('.toml', '.sh', '.bat', '.css', '.js', '.html'):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                c_count = 0
                for line in lines:
                    line_s = line.strip()
                    if ext in ('.toml', '.sh') and line_s.startswith('#') and chinese_pattern.search(line):
                        c_count += 1
                    elif ext == '.css' and '/*' in line and chinese_pattern.search(line):
                        c_count += 1
                if c_count:
                    stats[path] = {'type': ext, 'comments': c_count, 'docstrings': 0}
            except Exception:
                pass

print(f'Total files: {len(stats)}')
for p, s in sorted(stats.items(), key=lambda x: -(x[1]['comments'] + x[1]['docstrings'])):
    c = s['comments']
    d = s['docstrings']
    print(f"{p:50s} | comments: {c:3d} | docstrings: {d:3d} | total: {c+d:3d}")
