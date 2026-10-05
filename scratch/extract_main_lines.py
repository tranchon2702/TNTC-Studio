import sys, re, json

p = re.compile(r'[\u4e00-\u9fff]')
with open('webui/Main.py', 'r', encoding='utf-8') as f:
    lines = [l.strip() for l in f if p.search(l)]

unique_lines = list(dict.fromkeys(lines))
with open('scratch/main_chinese_lines.json', 'w', encoding='utf-8') as f:
    json.dump(unique_lines, f, ensure_ascii=False, indent=2)
print("Saved", len(unique_lines), "lines to scratch/main_chinese_lines.json")
