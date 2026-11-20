# README 引用检查器

在提交前找出 Markdown 中失效的本地图片、文档、附件和标题锚点。需要 Python 3.9 或更新版本，仅使用标准库，运行时不访问网络。

## 用法

```sh
python3 readme_ref_check.py README.md
python3 readme_ref_check.py --root . --json README.md docs/usage.md
```

可一次检查多个 UTF-8 Markdown 文件。相对链接从包含该链接的 Markdown 文件所在目录解析；以 `/` 开头的链接从 `--root` 指定的目录解析，默认使用当前工作目录。`--json` 输出诊断和计数，便于脚本处理。`--no-shortcuts` 跳过裸 `[标签]` 快捷式引用，适合正文中经常使用方括号的文档。

文本诊断格式为 `文件:行号:类别:原因:目标`。退出码 `0` 表示检查通过，`1` 表示发现失效引用，`2` 表示参数、读取或其他运行错误。

## 检查范围

- 检查行内链接和图片，以及完整式 `[文字][标签]`、折叠式 `[标签][]` 和快捷式 `[标签]` 引用；检查引用标签是否已定义。
- 检查本地目标是否存在，支持相对路径、中文、带空格的目标和 UTF-8 百分号编码。检查同页和跨文档的常见标题锚点，包括重复标题产生的后缀，例如 `#steps-1`。
- 跳过识别出的代码块和行内代码，不联网验证外部链接。对非 Markdown 文件只检查目标是否存在，不验证片段，例如 PDF 的 `#page=2`。

检查器确认的是当前工作区中的文件，不核对文件是否已纳入 Git 提交或暂存区。发布前还需确认引用的目标文件已纳入拟提交的版本。

检查器不是完整的 Markdown 渲染器。复杂的嵌套语法和特定平台生成的锚点可能与渲染结果不同。检查跨文档锚点时不会递归检查目标文档里的链接。

## 试跑示例

从 `readme_ref_check.py` 所在目录，在 Bash 等支持 here document 的 shell 中运行以下命令。示例会在临时目录创建 Markdown、图片和附件，并在运行结束后清理。

```sh
python3 - <<'PY'
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

checker = Path('readme_ref_check.py').resolve()
with TemporaryDirectory(prefix='readme-ref-demo-') as temp:
    root = Path(temp)
    (root / 'docs').mkdir()
    (root / 'assets').mkdir()
    (root / 'docs' / 'setup.md').write_text(
        '# Install\n\n## Steps\n\n## Steps\n', encoding='utf-8')
    (root / 'assets' / 'icon.svg').write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
        '<circle cx="8" cy="8" r="6"/></svg>\n', encoding='utf-8')
    (root / 'assets' / 'release notes.txt').write_text(
        'Example attachment\n', encoding='utf-8')
    (root / 'ok.md').write_text(
        '# Quick Start\n'
        '[Same page](#quick-start)\n'
        '![Icon](assets/icon.svg)\n'
        '[Guide][guide]\n'
        '[Attachment](/assets/release%20notes.txt)\n'
        '[Second Steps heading](docs/setup.md#steps-1)\n\n'
        '[guide]: docs/setup.md#install\n', encoding='utf-8')
    (root / 'broken.md').write_text(
        '# Broken example\n'
        '![Missing image](assets/missing.svg)\n'
        '[Missing heading](docs/setup.md#missing)\n', encoding='utf-8')

    for name, expected in (('ok.md', 0), ('broken.md', 1), ('missing.md', 2)):
        print(f'\n{name} (expected exit {expected})', flush=True)
        result = subprocess.run(
            [sys.executable, str(checker), '--root', str(root), name], cwd=root)
        print(f'exit: {result.returncode}', flush=True)
        if result.returncode != expected:
            raise SystemExit(f'{name}: expected {expected}, got {result.returncode}')
PY
```

预期 `ok.md` 没有问题；`broken.md` 第 2 行报告 `assets/missing.svg` 不存在，第 3 行报告 `docs/setup.md#missing` 的锚点不存在；`missing.md` 本身不存在，因此返回运行错误。

运行离线单元测试：`python3 readme_ref_check_tests.py`。
