# -*- coding: utf-8 -*-
"""
EPUB 构建脚本 - 将 VuePress 文档转为 EPUB 电子书。

本脚本在任意平台均可复用，仅需以下两项前置依赖:

前置依赖
--------
1. Python 3.x          (此脚本仅使用标准库, 无需安装额外 pip 包)
2. pandoc >= 3.0       https://pandoc.org/installing.html
   - Windows: scoop install pandoc 或从官网下载安装包
   - macOS:   brew install pandoc
   - Linux:   apt install pandoc  /  yum install pandoc

使用方法
--------
  yarn build:epub            # 通过 yarn
  python scripts/build_epub.py  # 或直接运行

构建流程
--------
  1. 解析 docs/.vuepress/config.js 的 sidebar，获得有序文件列表
  2. 逐文件预处理（VuePress 特有语法 -> pandoc 兼容 Markdown）
  3. 按序拼接为单一 Markdown 文件
  4. 调用 pandoc 生成 EPUB3
  5. 清理临时文件
"""

import re
import subprocess
import shutil
import sys
from pathlib import Path

# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = PROJECT_ROOT / 'docs'
PUBLIC_DIR = DOCS_DIR / '.vuepress' / 'public'
CONFIG_JS = DOCS_DIR / '.vuepress' / 'config.js'
TEMP_DIR = PROJECT_ROOT / 'temp_epub'
OUTPUT_FILE = PROJECT_ROOT / '深入理解-TypeScript.epub'
COVER_IMAGE = PUBLIC_DIR / 'logo.png'

# pandoc 元数据
BOOK_TITLE = '深入理解 TypeScript'
BOOK_AUTHOR = 'Basarat 著 . jkchao 等译'
BOOK_LANGUAGE = 'zh-CN'


# ------------------------------------------------------------
# 1. 从 config.js 解析 sidebar，提取有序文件列表
# ------------------------------------------------------------
def parse_sidebar(config_js_path):
    """
    从 VuePress config.js 中提取 sidebar 里所有的 children 路径。
    返回按序排列的文件路径列表（相对于 DOCS_DIR）。
    每个路径形如 '/project/compilationContext' -> 'project/compilationContext.md'
    """

    text = config_js_path.read_text(encoding='utf-8')

    # 定位 sidebar 数组内容 - 从 'sidebar: [' 开始
    sidebar_match = re.search(r'sidebar\s*:\s*\[', text)
    if not sidebar_match:
        print('[错误] 未能在 config.js 中找到 sidebar 定义')
        sys.exit(1)

    # 提取 sidebar 数组的完整内容（匹配大括号）
    start = sidebar_match.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == '[':
            depth += 1
        elif text[i] == ']':
            depth -= 1
        i += 1
    sidebar_body = text[start:i - 1]

    # 移除 JS 行注释 (// ...)，避免匹配到被注释的 sidebar 项
    sidebar_body = re.sub(r'//.*', '', sidebar_body)

    # 从 children 数组中提取所有路径
    children_re = re.compile(
        r"children\s*:\s*\[(.*?)\]",
        re.DOTALL
    )
    # 匹配引号中的路径: 'xxx' 或 "xxx"
    path_re = re.compile(r"""['"](\.\/)?([^'"]+?)['"]""")

    files = []
    seen = set()
    for children_match in children_re.finditer(sidebar_body):
        children_text = children_match.group(1)
        for path_match in path_re.finditer(children_text):
            relative = path_match.group(2)
            if relative == '' or relative == '/':
                f = 'README.md'
            else:
                relative = relative.lstrip('/')
                if not relative.endswith('.md'):
                    relative += '.md'
                f = relative
            if f not in seen:
                seen.add(f)
                files.append(f)

    if not files:
        print('[错误] sidebar 解析结果为空，请检查 config.js 格式')
        sys.exit(1)

    print('[信息] 从 sidebar 中解析到 %d 个文件' % len(files))
    return files


# ------------------------------------------------------------
# 2. 预处理单个 Markdown 文件
# ------------------------------------------------------------
def preprocess_md(file_path, public_dir):
    """
    对单个 .md 文件做预处理，返回处理后的文本。
    处理项:
      - $withBase('/path') -> 替换为实际文件路径
      - <img :src="$withBase('/path')" ...> -> 标准 Markdown 图片
      - ::: tip|warning|danger -> pandoc fenced div 语法 ::: {.class}
      - 跨文件链接 [text](./other.md) -> 仅保留文字
      - 移除 <!-- ALL-CONTRIBUTORS-LIST:... --> 区块
    """

    raw = file_path.read_text(encoding='utf-8')

    # 2a. 移除 all-contributors 区块（首尾标记之间所有内容）
    raw = re.sub(
        r'<!-- ALL-CONTRIBUTORS-LIST:START.*?ALL-CONTRIBUTORS-LIST:END -->',
        '',
        raw,
        flags=re.DOTALL
    )

    # 2b. 处理 <img :src="$withBase('/path')" ...> -> ![alt](相对路径)
    def replace_img_with_base(m):
        src_match = re.search(r"\$withBase\('([^']+)'\)", m.group(0))
        alt_match = re.search(r'alt="([^"]*)"', m.group(0))
        if src_match:
            img_path = src_match.group(1).lstrip('/')
            alt_text = alt_match.group(1) if alt_match else ''
            rel_path = Path('docs') / '.vuepress' / 'public' / img_path
            return '![%s](%s)' % (alt_text, rel_path.as_posix())
        return m.group(0)

    raw = re.sub(r'<img\s[^>]*\$withBase\([^>]+>', replace_img_with_base, raw)

    # 2c. 处理裸 $withBase('...') 在 markdown 正文中的情况
    def replace_withbase(m):
        path = m.group(1).lstrip('/')
        rel_path = Path('docs') / '.vuepress' / 'public' / path
        return rel_path.as_posix()

    raw = re.sub(r"\$withBase\('([^']+)'\)", replace_withbase, raw)

    # 2d. 处理 VuePress 自定义容器 -> pandoc fenced div
    def replace_container_open(m):
        directive = m.group(1).strip()
        main_type = directive.split()[0].lower()
        if main_type in ('tip', 'warning', 'danger'):
            return '::: {.%s}' % main_type
        return ':::'

    raw = re.sub(
        r'^:::\s*(tip|warning|danger)(.*)$',
        replace_container_open,
        raw,
        flags=re.MULTILINE | re.IGNORECASE
    )

    # 2e. 跨文件 Markdown 链接: [text](./other.md) 或 [text](other.md)
    raw = re.sub(
        r'\[([^\]]*?)\]\(\.\/[^)]+\.md(?:#[^)]*)?\)',
        r'\1',
        raw
    )
    raw = re.sub(
        r'\[([^\]]*?)\]\([^)]+\.md(?:#[^)]*)?\)',
        r'\1',
        raw
    )

    # 2f. 处理 .html 链接（部分 FAQ 中使用了 .html 后缀）
    raw = re.sub(
        r'\[([^\]]*?)\]\(\.\/[^)]+\.html(?:#[^)]*)?\)',
        r'\1',
        raw
    )

    # 2g. 清理多余的空白行（多个连续空行 -> 最多两个）
    raw = re.sub(r'\n{4,}', '\n\n\n', raw)

    return raw


# ------------------------------------------------------------
# 3. 工具函数
# ------------------------------------------------------------
def extract_first_heading(text):
    """提取文件的第一个 # 标题文字（用于日志）"""
    m = re.search(r'^#\s+(.+)$', text, re.MULTILINE)
    return m.group(1).strip() if m else '(无标题)'


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def main():
    print('=' * 50)
    print('  深入理解 TypeScript - EPUB 构建工具')
    print('=' * 50)

    # 步骤1: 解析 sidebar
    print('\n[步骤 1/5] 解析 config.js sidebar ...')
    ordered_files = parse_sidebar(CONFIG_JS)
    for f in ordered_files:
        print('  - ' + f)

    # 校验所有文件存在
    missing = [f for f in ordered_files if not (DOCS_DIR / f).exists()]
    if missing:
        print('[错误] 以下文件不存在: %s' % missing)
        sys.exit(1)

    # 步骤2: 创建临时目录并预处理
    print('\n[步骤 2/5] 预处理 Markdown 文件 ...')
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    TEMP_DIR.mkdir(parents=True)

    processed_pages = []
    for filename in ordered_files:
        src = DOCS_DIR / filename
        processed = preprocess_md(src, PUBLIC_DIR)
        heading = extract_first_heading(processed)
        processed_pages.append(processed)
        print('  + %s  ->  "%s"' % (filename, heading))

    # 步骤3: 拼接所有文件
    print('\n[步骤 3/5] 拼接为单一文档 ...')
    all_md = '\n\n\\newpage\n\n'.join(processed_pages)
    all_md = all_md.lstrip('\n')

    merged_file = TEMP_DIR / 'all.md'
    merged_file.write_text(all_md, encoding='utf-8')
    print('  [写入] %s（%d 字符）' % (merged_file, len(all_md)))

    # 步骤4: 调用 pandoc 生成 EPUB
    print('\n[步骤 4/5] 调用 pandoc 生成 EPUB ...')

    # 检查 pandoc 是否可用
    try:
        subprocess.run(['pandoc', '--version'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print('[错误] pandoc 未安装或不在 PATH 中。请安装 pandoc: https://pandoc.org/')
        sys.exit(1)

    cover_arg = []
    if COVER_IMAGE.exists():
        cover_arg = ['--epub-cover-image', str(COVER_IMAGE)]
        print('  [封面图片] ' + str(COVER_IMAGE))
    else:
        print('  [跳过] 封面图片不存在: ' + str(COVER_IMAGE))

    css_file = PROJECT_ROOT / 'styles' / 'epub.css'
    css_arg = []
    if css_file.exists():
        css_arg = ['--css', str(css_file)]
        print('  [自定义样式] ' + str(css_file))
    else:
        print('  [跳过] 样式文件不存在: ' + str(css_file))

    cmd = [
        'pandoc',
        str(merged_file),
        '--from=markdown+smart',
        '--to=epub3',
        '--toc',
        '--toc-depth=3',
        '--split-level=1',
        '--metadata', 'title=' + BOOK_TITLE,
        '--metadata', 'author=' + BOOK_AUTHOR,
        '--metadata', 'lang=' + BOOK_LANGUAGE,
        '--output', str(OUTPUT_FILE),
    ] + cover_arg + css_arg

    print('\n  正在生成 EPUB，请稍候 ...')
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print('[错误] pandoc 执行失败 (exit %d)' % result.returncode)
        if result.stdout:
            print('  stdout: ' + result.stdout)
        if result.stderr:
            print('  stderr: ' + result.stderr)
        sys.exit(1)

    if result.stdout:
        print('  pandoc 输出: ' + result.stdout)
    if result.stderr:
        print('  pandoc 警告: ' + result.stderr)

    # 步骤5: 清理临时文件
    print('\n[步骤 5/5] 清理临时文件 ...')
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
        print('  [临时目录已删除]')

    # 输出结果
    if OUTPUT_FILE.exists():
        size_kb = OUTPUT_FILE.stat().st_size / 1024
        print('\n' + '=' * 50)
        print('  [完成] EPUB 生成成功！')
        print('  输出: ' + str(OUTPUT_FILE))
        print('  大小: %.1f KB' % size_kb)
        print('=' * 50)
    else:
        print('\n[错误] 输出文件未生成: ' + str(OUTPUT_FILE))
        sys.exit(1)


if __name__ == '__main__':
    main()
