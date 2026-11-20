#!/usr/bin/env python3
"""Offline Markdown reference checker. Python 3.9+, standard library only.

Exit codes: 0 = passed, 1 = broken references, 2 = read/runtime error.
This is a deliberately bounded Markdown parser, not a full CommonMark renderer.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
import html
import json
from pathlib import Path
import re
import string
import sys
import unicodedata
from urllib.parse import unquote


MARKDOWN_SUFFIXES = {'.md', '.markdown', '.mdown'}
SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
ESCAPE = re.compile(r'\\([' + re.escape(string.punctuation) + r'])')
TITLE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\((?:\\.|[^)\\])*\))'''
TITLE_ONLY = re.compile(TITLE + r'\s*\Z', re.S)
TRAILING_TITLE = re.compile(r'\s+' + TITLE + r'\s*\Z', re.S)
DEFINITION = re.compile(r'^ {0,3}\[((?:\\.|[^\]\\\n])+)\]:[ \t]*(.*)$')
ATX = re.compile(r'^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$')
SETEXT = re.compile(r'^ {0,3}(?:=+|-+)[ \t]*$')
LIST_START = re.compile(r'^\s*(?:[-+*]|\d+[.)])[ \t]+')


@dataclass(frozen=True)
class Issue:
    file: str
    line: int
    target: str
    reason: str
    kind: str = 'broken'


@dataclass
class CheckResult:
    issues: list[Issue] = field(default_factory=list)
    checked: int = 0
    external_skipped: int = 0
    fragments_skipped: int = 0

    @property
    def exit_code(self) -> int:
        if any(issue.kind == 'error' for issue in self.issues):
            return 2
        return 1 if self.issues else 0


@dataclass(frozen=True)
class Reference:
    offset: int
    target: str | None = None
    label: str | None = None


@dataclass
class Document:
    path: Path
    text: str
    definitions: dict[str, str]
    references: list[Reference]
    anchors: set[str]
    newlines: list[int]

    def line_number(self, offset: int) -> int:
        return bisect_right(self.newlines, offset) + 1


def blank(text: str) -> str:
    return ''.join('\n' if c == '\n' else ' ' for c in text)


def unescape(text: str) -> str:
    return html.unescape(ESCAPE.sub(r'\1', text))


def normalize_label(text: str) -> str:
    return ' '.join(unescape(text).split()).casefold()


def closing_ticks(text: str, start: int) -> tuple[int, int] | None:
    end = start
    while end < len(text) and text[end] == '`':
        end += 1
    width = end - start
    match = re.search(r'(?<!`)`{' + str(width) + r'}(?!`)', text[end:])
    return (end + match.start(), end + match.end()) if match else None


def block_view(text: str) -> str:
    """Blank fences, indented code and comments while preserving offsets."""
    result = []
    fence_char = ''
    fence_width = 0
    comment = False
    for line in text.splitlines(keepends=True):
        content = line.rstrip('\r\n')
        if fence_char:
            if re.fullmatch(r' {0,3}' + re.escape(fence_char) +
                            r'{' + str(fence_width) + r',}[ \t]*', content):
                fence_char = ''
            result.append(blank(line))
            continue
        opening = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', content)
        if not comment and opening and not (
                opening.group(1)[0] == '`' and '`' in opening.group(2)):
            fence_char = opening.group(1)[0]
            fence_width = len(opening.group(1))
            result.append(blank(line))
            continue
        if not comment and re.match(r'^(?: {4}|\t)', line):
            result.append(blank(line))
            continue
        chars = list(line)
        i = 0
        while i < len(line):
            if comment:
                end = line.find('-->', i)
                stop = len(line) if end < 0 else end + 3
                chars[i:stop] = blank(line[i:stop])
                i = stop
                if end >= 0:
                    comment = False
            elif line.startswith('<!--', i):
                comment = True
            elif line[i] == '`' and (close := closing_ticks(line, i)):
                i = close[1]
            elif line[i] == '\\':
                i += 2
            else:
                i += 1
        result.append(''.join(chars))
    return ''.join(result)


def inline_view(text: str) -> str:
    chars = list(text)
    i = 0
    while i < len(text):
        if text[i] == '\\':
            i += 2
        elif text[i] == '`':
            close = closing_ticks(text, i)
            if close:
                chars[i:close[1]] = blank(text[i:close[1]])
                i = close[1]
            else:
                while i < len(text) and text[i] == '`':
                    i += 1
        else:
            i += 1
    return ''.join(chars)


def bracket_end(text: str, start: int) -> int | None:
    depth = 1
    i = start + 1
    while i < len(text):
        if text[i] == '\\':
            i += 2
            continue
        if text[i] == '[':
            depth += 1
        elif text[i] == ']':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def parenthesis_end(text: str, start: int) -> int | None:
    depth = 1
    quote = ''
    angle = False
    i = start + 1
    while i < len(text):
        c = text[i]
        if c == '\\':
            i += 2
            continue
        if quote:
            if c == quote:
                quote = ''
        elif angle:
            if c == '>':
                angle = False
        elif c == '<' and not text[start + 1:i].strip():
            angle = True
        elif c in ('"', "'") and i > start + 1 and text[i - 1].isspace():
            quote = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def destination(payload: str) -> str | None:
    payload = payload.strip()
    if payload.startswith('<'):
        i = 1
        while i < len(payload):
            if payload[i] == '\\':
                i += 2
                continue
            if payload[i] == '>':
                tail = payload[i + 1:].strip()
                if tail and not TITLE_ONLY.fullmatch(tail):
                    return None
                return payload[1:i]
            i += 1
        return None
    title = TRAILING_TITLE.search(payload)
    return payload[:title.start()].rstrip() if title else payload


def definitions_from(text: str) -> tuple[dict[str, str], list[tuple[int, int]]]:
    lines = text.splitlines(keepends=True)
    offsets = []
    total = 0
    for line in lines:
        offsets.append(total)
        total += len(line)
    definitions = {}
    spans = []
    i = 0
    while i < len(lines):
        match = DEFINITION.match(lines[i].rstrip('\r\n'))
        if not match:
            i += 1
            continue
        first = i
        payload = match.group(2).strip()
        if not payload and i + 1 < len(lines) and lines[i + 1].strip():
            i += 1
            payload = lines[i].strip()
        if not payload:
            i += 1
            continue
        target = destination(payload)
        if target is None:
            i += 1
            continue
        if i + 1 < len(lines) and TITLE_ONLY.fullmatch(lines[i + 1].strip()):
            i += 1
        label = normalize_label(match.group(1))
        definitions.setdefault(label, target)
        spans.append((offsets[first], offsets[i] + len(lines[i])))
        i += 1
    return definitions, spans


def is_task_marker(view: str, start: int, end: int, label: str) -> bool:
    if label not in (' ', 'x', 'X'):
        return False
    prefix = view[view.rfind('\n', 0, start) + 1:start]
    return bool(re.fullmatch(r' {0,3}(?:[-+*]|\d+[.)])[ \t]+', prefix)) and (
        end + 1 == len(view) or view[end + 1].isspace())


def references_from(view: str, raw: str, shortcuts: bool = True,
                    base: int = 0, images_only: bool = False) -> list[Reference]:
    refs = []
    i = 0
    while i < len(view):
        if view[i] == '\\':
            i += 2
            continue
        image = view.startswith('![', i)
        if not image and (view[i] != '[' or images_only):
            i += 1
            continue
        start = i
        opening = i + 1 if image else i
        end = bracket_end(view, opening)
        if end is None:
            i += 1
            continue
        label = raw[opening + 1:end]
        following = end + 1
        ref = None
        stop = following
        if following < len(view) and view[following] == '(':
            close = parenthesis_end(raw, following)
            if close is not None:
                target = destination(raw[following + 1:close])
                if target is not None:
                    ref = Reference(base + start, target=target)
                    stop = close + 1
        if ref is None:
            j = following
            while j < len(view) and view[j] in ' \t\r\n':
                j += 1
            gap = view[following:j]
            if gap.count('\n') <= 1 and j < len(view) and view[j] == '[':
                close = bracket_end(view, j)
                if close is not None:
                    explicit = raw[j + 1:close]
                    ref = Reference(base + start, label=normalize_label(explicit or label))
                    stop = close + 1
            if ref is None and shortcuts and not (
                    not image and is_task_marker(view, opening, end, label)):
                ref = Reference(base + start, label=normalize_label(label))
        if ref is not None:
            refs.append(ref)
            refs.extend(references_from(view[opening + 1:end],
                                        raw[opening + 1:end], shortcuts,
                                        base + opening + 1, True))
        i = max(stop, end + 1)
    return refs


def heading_text(text: str, definitions: dict[str, str]) -> str:
    result = []
    i = 0
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text):
            result.append(text[i + 1])
            i += 2
            continue
        image = text.startswith('![', i)
        opening = i + 1 if image else i
        if opening < len(text) and text[opening] == '[':
            close = bracket_end(text, opening)
            if close is not None:
                label = text[opening + 1:close]
                stop = close + 1
                linked = normalize_label(label) in definitions
                if stop < len(text) and text[stop] == '(':
                    end = parenthesis_end(text, stop)
                    if end is not None:
                        stop = end + 1
                        linked = True
                elif stop < len(text) and text[stop] == '[':
                    end = bracket_end(text, stop)
                    if end is not None:
                        ref_label = text[stop + 1:end] or label
                        if normalize_label(ref_label) in definitions:
                            stop = end + 1
                            linked = True
                if linked:
                    if not image:
                        result.append(heading_text(label, definitions))
                    i = stop
                    continue
        result.append(text[i])
        i += 1
    rendered = ''.join(result)
    rendered = re.sub(r'<[^>]*>', '', rendered)
    rendered = rendered.replace('`', '')
    rendered = re.sub(r'(?<!\w)_{1,3}(.+?)_{1,3}(?!\w)', r'\1', rendered)
    return html.unescape(rendered)


def slug(text: str, definitions: dict[str, str]) -> str:
    text = heading_text(text, definitions).strip().lower()
    chars = []
    for c in text:
        if c.isspace():
            chars.append('-')
        elif c in '_-' or unicodedata.category(c)[0] not in 'PSC':
            chars.append(c)
    return ''.join(chars)


def anchors_from(view: str, definitions: dict[str, str]) -> set[str]:
    anchors = set()
    used = set()
    counters = {}
    previous = ''
    for line in view.splitlines():
        match = ATX.fullmatch(line)
        title = None
        if match:
            title = re.sub(r'[ \t]+#+[ \t]*$', '', match.group(2) or '')
            previous = ''
        elif SETEXT.fullmatch(line) and previous.strip() and not LIST_START.match(previous):
            title = previous.strip()
            previous = ''
        else:
            previous = line if line.strip() else ''
        if title is not None:
            base = slug(title, definitions)
            candidate = base
            while candidate in used:
                counters[base] = counters.get(base, 0) + 1
                candidate = base + '-' + str(counters[base])
            used.add(candidate)
            anchors.add(candidate)
    # Explicit HTML id attributes, and legacy <a name="..."> anchors.
    for tag in re.finditer(r'<([A-Za-z][\w:-]*)\b([^>]*)>', inline_view(view)):
        pattern = r'''\b(id|name)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))'''
        for attr in re.finditer(pattern, tag.group(2), re.I):
            if attr.group(1).lower() == 'id' or tag.group(1).lower() == 'a':
                anchors.add(html.unescape(next(v for v in attr.groups()[1:] if v is not None)))
    return anchors


def parse_document(path: Path, text: str, shortcuts: bool = True) -> Document:
    blocks = block_view(text)
    definitions, spans = definitions_from(blocks)
    chars = list(inline_view(blocks))
    heading_chars = list(blocks)
    for start, end in spans:
        chars[start:end] = blank(blocks[start:end])
        heading_chars[start:end] = blank(blocks[start:end])
    return Document(path, text, definitions,
                    references_from(''.join(chars), text, shortcuts),
                    anchors_from(''.join(heading_chars), definitions),
                    [i for i, c in enumerate(text) if c == '\n'])


class Checker:
    def __init__(self, root: Path | None = None, shortcuts: bool = True):
        self.root = (root or Path.cwd()).resolve()
        self.shortcuts = shortcuts
        self.cache: dict[Path, Document] = {}
        self.read_errors: dict[Path, str] = {}

    def document(self, path: Path) -> Document:
        path = path.resolve()
        if path in self.read_errors:
            raise OSError(self.read_errors[path])
        if path not in self.cache:
            try:
                text = path.read_text(encoding='utf-8-sig')
            except (OSError, UnicodeError) as error:
                self.read_errors[path] = str(error)
                raise OSError(str(error)) from error
            self.cache[path] = parse_document(path, text, self.shortcuts)
        return self.cache[path]

    def check(self, path: Path | str) -> CheckResult:
        display = str(path)
        path = Path(path)
        result = CheckResult()
        try:
            document = self.document(path)
        except (OSError, ValueError, RuntimeError) as error:
            result.issues.append(Issue(display, 1, str(path),
                                       'cannot read input: ' + str(error), 'error'))
            return result
        for ref in document.references:
            line = document.line_number(ref.offset)
            if ref.label is not None:
                if ref.label not in document.definitions:
                    result.issues.append(Issue(display, line, '[' + ref.label + ']',
                                               'undefined reference label'))
                    continue
                raw_target = document.definitions[ref.label]
            else:
                raw_target = ref.target if ref.target is not None else ''
            target = unescape(raw_target)
            if SCHEME.match(target) or target.startswith('//'):
                result.external_skipped += 1
                continue
            result.checked += 1
            file_part, separator, fragment = target.partition('#')
            file_part = file_part.partition('?')[0]
            try:
                decoded_path = unquote(file_part, encoding='utf-8', errors='strict')
                decoded_fragment = unquote(fragment, encoding='utf-8', errors='strict')
                if '\x00' in decoded_path or '\x00' in decoded_fragment:
                    raise ValueError('NUL character in reference')
            except (UnicodeError, ValueError) as error:
                result.issues.append(Issue(display, line, raw_target,
                                           'invalid reference encoding: ' + str(error)))
                continue
            if not decoded_path:
                local = document.path
            elif decoded_path.startswith('/'):
                local = self.root / decoded_path.lstrip('/')
            else:
                local = document.path.parent / decoded_path
            try:
                local.stat()
            except (FileNotFoundError, NotADirectoryError):
                result.issues.append(Issue(display, line, raw_target, 'local target does not exist'))
                continue
            except (OSError, ValueError) as error:
                result.issues.append(Issue(display, line, raw_target,
                                           'cannot inspect target: ' + str(error), 'error'))
                continue
            if not separator or not decoded_fragment:
                continue
            if decoded_path and local.suffix.lower() not in MARKDOWN_SUFFIXES:
                result.fragments_skipped += 1
                continue
            try:
                target_document = self.document(local)
            except (OSError, ValueError, RuntimeError) as error:
                result.issues.append(Issue(display, line, raw_target,
                                           'cannot read Markdown target: ' + str(error), 'error'))
                continue
            if decoded_fragment not in target_document.anchors:
                result.issues.append(Issue(display, line, raw_target,
                                           'Markdown heading or explicit HTML anchor does not exist'))
        return result


def check_file(path: Path | str, root: Path | None = None,
               shortcuts: bool = True) -> CheckResult:
    return Checker(root, shortcuts).check(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path, help='UTF-8 Markdown files to check')
    parser.add_argument('--root', type=Path, default=Path.cwd(),
                        help='base directory for /root-relative links (default: current directory)')
    parser.add_argument('--no-shortcuts', action='store_true',
                        help='ignore bare [label] shortcut references to reduce prose false positives')
    parser.add_argument('--json', action='store_true', dest='json_output',
                        help='print machine-readable results')
    args = parser.parse_args(argv)
    try:
        if not args.root.is_dir():
            parser.error('--root must be an existing directory')
        checker = Checker(args.root, not args.no_shortcuts)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    combined = CheckResult()
    for path in args.files:
        result = checker.check(path)
        combined.issues.extend(result.issues)
        combined.checked += result.checked
        combined.external_skipped += result.external_skipped
        combined.fragments_skipped += result.fragments_skipped
    if args.json_output:
        print(json.dumps({'issues': [asdict(issue) for issue in combined.issues],
                          'checked': combined.checked,
                          'external_skipped': combined.external_skipped,
                          'fragments_skipped': combined.fragments_skipped,
                          'exit_code': combined.exit_code}, ensure_ascii=False, indent=2))
    else:
        for issue in combined.issues:
            print(f'{issue.file}:{issue.line}: {issue.kind}: {issue.reason}: {issue.target!r}')
        print(f'{combined.checked} local references checked; '
              f'{combined.external_skipped} external references skipped; '
              f'{combined.fragments_skipped} fragments on non-Markdown targets skipped; '
              f'{len(combined.issues)} issues')
    return combined.exit_code


if __name__ == '__main__':
    sys.exit(main())
