"""Run with: python readme_ref_check_tests.py"""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote


CHECKER = Path(__file__).resolve().with_name('readme_ref_check.py')


class ReferenceCheckerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, name, text=''):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def check(self, path='README.md'):
        env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR')
               if key in os.environ}
        return subprocess.run(
            [sys.executable, '-I', '-B', '-X', 'utf8', str(CHECKER), path],
            cwd=self.root, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, encoding='utf-8', timeout=10,
        )

    @staticmethod
    def output(result):
        return result.stdout + '\n' + result.stderr

    def assert_exit(self, result, expected):
        self.assertEqual(result.returncode, expected, self.output(result))

    def assert_issue(self, result, path, line, target):
        output = self.output(result)
        location = re.escape(path) + rf':{line}(?!\d)'
        self.assertTrue(
            any(re.search(location, row) and target in row
                for row in output.splitlines()),
            f'Expected {path}:{line} to report {target!r}; output:\n{output}',
        )

    def test_existing_images_documents_attachments_and_encoded_paths(self):
        self.write('assets/封面.png').write_bytes(b'image fixture')
        self.write('docs/用户 指南.md', '# 说明\n')
        attachment = '报告 (最终).zip'
        self.write('files/' + attachment).write_bytes(b'attachment fixture')
        self.write('guide/README.md',
                   '# Demo\n\n'
                   '![封面](../assets/封面.png)\n\n'
                   '[文档](<../docs/用户 指南.md> "指南")\n\n'
                   f'[附件](../files/{quote(attachment)})\n\n'
                   '[External](https://example.invalid/not-a-file)\n')
        self.assert_exit(self.check('guide/README.md'), 0)

    def test_missing_files_report_the_reference_line_and_target(self):
        self.write('README.md', '# Demo\n\n'
                   '![Old cover](images/old.png)\n\n'
                   '[Old manual](docs/gone.md)\n\n'
                   '[Old download](files/archive.zip)\n')
        result = self.check()
        self.assert_exit(result, 1)
        for line, target in [(3, 'images/old.png'), (5, 'docs/gone.md'),
                             (7, 'files/archive.zip')]:
            with self.subTest(target=target):
                self.assert_issue(result, 'README.md', line, target)

    def test_full_collapsed_and_shortcut_reference_links(self):
        self.write('guide.md', '# Guide\n')
        self.write('download.zip')
        self.write('LICENSE.txt')
        self.write('cover.png')
        self.write('README.md',
                   '[Read the docs][doc label]\n\n'
                   '[Download][]\n\n'
                   '[License]\n\n'
                   '![Cover][image]\n\n'
                   '[DoC   Label]: guide.md\n'
                   '[download]: download.zip\n'
                   '[license]: LICENSE.txt\n'
                   '[image]: cover.png\n')
        self.assert_exit(self.check(), 0)

    def test_undefined_reference_labels_report_each_use(self):
        self.write('README.md',
                   '[Text][missing-full]\n\n'
                   '[missing-collapsed][]\n\n'
                   '[missing-short]\n\n'
                   '![Alt][missing-image]\n')
        result = self.check()
        self.assert_exit(result, 1)
        for line, label in [(1, 'missing-full'), (3, 'missing-collapsed'),
                            (5, 'missing-short'), (7, 'missing-image')]:
            with self.subTest(label=label):
                self.assert_issue(result, 'README.md', line, label)

    def test_same_page_cross_document_and_duplicate_heading_anchors(self):
        self.write('docs/Guide.md',
                   '# Quick `Start`!\n\n## Quick Start\n\n其他 选项\n---------\n')
        self.write('README.md',
                   '# 开始 使用\n\n'
                   '[开始](#开始-使用)\n\n'
                   '[第二个同名标题](#repeat-1)\n\n'
                   '[Quick Start](docs/Guide.md#quick-start)\n\n'
                   '[Quick Start again](docs/Guide.md#quick-start-1)\n\n'
                   f'[其他选项](docs/Guide.md#{quote("其他-选项")})\n\n'
                   '## Repeat\n\n## Repeat\n')
        self.assert_exit(self.check(), 0)

    def test_missing_anchors_even_when_target_document_exists(self):
        self.write('docs/guide.md', '# Real heading\n')
        self.write('README.md', '# Demo\n\n'
                   '[Wrong here](#fence-title)\n\n'
                   '[Wrong there](docs/guide.md#absent-anchor)\n\n'
                   '```markdown\n# Fence title\n```\n')
        result = self.check()
        self.assert_exit(result, 1)
        self.assert_issue(result, 'README.md', 3, '#fence-title')
        self.assert_issue(result, 'README.md', 5, 'docs/guide.md#absent-anchor')

    def test_ignores_fenced_indented_and_inline_code_examples(self):
        self.write('present.md', '# Present\n')
        self.write('README.md',
                   '# Examples\n\n'
                   '```markdown\n![Example](fenced-missing.png)\n```\n\n'
                   '~~~\n[Example][missing-label]\n~~~\n\n'
                   '    [Example](indented-missing.md)\n\n'
                   'Compare `` `[example]` [fake](inline-missing.md) `` with '
                   '`![example](inline-missing.png)`.\n\n'
                   '[Real link](present.md)\n')
        self.assert_exit(self.check(), 0)

    def test_missing_or_invalid_utf8_input_is_a_read_error(self):
        self.write('invalid.md').write_bytes(b'\xff\xfe')
        for path in ('nonexistent.md', 'invalid.md'):
            with self.subTest(path=path):
                result = self.check(path)
                self.assert_exit(result, 2)
                self.assertIn(path, self.output(result))


if __name__ == '__main__':
    unittest.main(verbosity=2)
