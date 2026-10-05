import sys, ast
sys.stdout.reconfigure(encoding='utf-8')

with open('app/services/bgm.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

tree = ast.parse(''.join(lines))
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            doc = node.body[0].value
            import re
            if re.search(r'[\u4e00-\u9fff]', doc.value):
                name = getattr(node, 'name', 'module')
                print(f"--- {name} (lines {doc.lineno}-{doc.end_lineno}) ---")
                for lno in range(doc.lineno, doc.end_lineno + 1):
                    print(f"{lno:3d}: {lines[lno-1].rstrip()}")
