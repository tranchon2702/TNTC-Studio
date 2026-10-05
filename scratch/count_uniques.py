import sys, os, re, tokenize, ast
sys.stdout.reconfigure(encoding='utf-8')

chinese_pattern = re.compile(r'[\u4e00-\u9fff]')
ignore_dirs = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', 'scratch'}

unique_comments = set()
unique_docstrings = set()
other_comments = set()

for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ignore_dirs]
    for file in files:
        path = os.path.join(root, file)
        ext = os.path.splitext(file)[1]
        
        if ext == '.py':
            try:
                with open(path, 'rb') as f:
                    tokens = list(tokenize.tokenize(f.readline))
                for tok in tokens:
                    if tok.type == tokenize.COMMENT and chinese_pattern.search(tok.string):
                        unique_comments.add(tok.string)
            except Exception:
                pass
            
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    tree = ast.parse(f.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                        doc = ast.get_docstring(node, clean=False)
                        if doc and chinese_pattern.search(doc):
                            unique_docstrings.add(doc)
            except Exception:
                pass
        
        elif ext in ('.toml', '.sh', '.bat', '.css'):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                for line in lines:
                    line_s = line.strip()
                    if ext in ('.toml', '.sh') and line_s.startswith('#') and chinese_pattern.search(line):
                        other_comments.add(line_s)
                    elif ext == '.css' and '/*' in line and chinese_pattern.search(line):
                        other_comments.add(line_s)
            except Exception:
                pass

print(f"Unique Python comments with Chinese: {len(unique_comments)}")
print(f"Unique Python docstrings with Chinese: {len(unique_docstrings)}")
print(f"Unique other file comments with Chinese: {len(other_comments)}")
print(f"Total unique texts to translate: {len(unique_comments) + len(unique_docstrings) + len(other_comments)}")
