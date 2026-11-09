# -*- coding: utf-8 -*-
"""Build the APT Packages index from debs/.

Shared by .github/workflows/release.yml (commits the result back to the repo)
and .github/workflows/deploy-pages.yml (deploys the freshly built site).
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLEAN_NAME = re.compile(r'[（(]\s*免授权\s*[)）]')
ICON_BASE = 'https://ynyy0.github.io/icons/'
DEFAULT_ICON = 'https://ynyy0.github.io/CydiaIcon.png'
META_FILE = ROOT / 'repo-meta.json'


def load_meta():
    """Per-package display overrides written by the web console."""
    if not META_FILE.exists():
        return {}
    try:
        return json.loads(META_FILE.read_text(encoding='utf-8')).get('plugins', {})
    except Exception:
        return {}


def find_override(overrides, package):
    """精确匹配优先，再兼容 deb control 大小写差异。"""
    if package in overrides:
        return overrides[package]
    for key, value in overrides.items():
        if key.lower() == package.lower():
            return value
    return {}


def icon_version(path):
    """图标内容哈希：换图后 URL 变化，客户端不会拿旧缓存。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    except Exception:
        return '0'


def main():
    overrides = load_meta()
    result = subprocess.run(
        ['dpkg-scanpackages', '-m', 'debs', '/dev/null'],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    stanzas = [s for s in result.stdout.rstrip('\n').split('\n\n') if s.strip()]
    updated = []

    for stanza in stanzas:
        lines = stanza.splitlines()
        # 去掉所有插件名称里残留的「（免授权）」标记
        lines = [
            'Name: ' + CLEAN_NAME.sub('', line[6:]).strip()
            if line.startswith('Name: ') else line
            for line in lines
        ]
        package = next(
            (line.split(':', 1)[1].strip() for line in lines
             if line.startswith('Package: ')),
            None,
        )
        if package:
            icon_path = ROOT / 'icons' / f'{package}.png'
            if not icon_path.is_file():
                # deb control 里的包名大小写可能与索引不一致
                matches = [p for p in (ROOT / 'icons').glob('*.png')
                           if p.stem.lower() == package.lower()]
                if matches:
                    icon_path = matches[0]
            if icon_path.is_file():
                lines = [line for line in lines if not line.startswith('Icon: ')]
                lines.append(f'Icon: {ICON_BASE}{icon_path.name}?v={icon_version(icon_path)}')
            elif not any(line.startswith('Icon: ') for line in lines):
                lines.append(f'Icon: {DEFAULT_ICON}')

        if package == 'iya.banana.ios27animation':
            lines = [
                'Section: 公益插件' if line.startswith('Section: ') else line
                for line in lines
            ]
            lines = [
                'Description: 与你有源 / 已公益' if line.startswith('Description: ') else line
                for line in lines
            ]
            if not any(line.startswith('Section: ') for line in lines):
                lines.append('Section: 公益插件')

        # 管理台写入的显示名 / 描述覆盖，优先级最高
        ov = find_override(overrides, package)
        if ov.get('name'):
            lines = [line for line in lines if not line.startswith('Name: ')]
            lines.append('Name: ' + str(ov['name']))
        if ov.get('description'):
            lines = [line for line in lines if not line.startswith('Description: ')]
            lines.append('Description: ' + str(ov['description']))
        # 管理台里把插件移动到某个分类目录后，源索引的 Section 同步改成该分类
        if ov.get('section'):
            lines = [line for line in lines if not line.startswith('Section: ')]
            lines.append('Section: ' + str(ov['section']))

        updated.append('\n'.join(lines))

    (ROOT / 'Packages').write_text('\n\n'.join(updated) + '\n\n', encoding='utf-8')

    homepage = ROOT / 'index.html'
    if homepage.exists():
        html = homepage.read_text(encoding='utf-8')
        html = html.replace('于你有·源', '与你有源')
        homepage.write_text(html, encoding='utf-8')

    print(f'Indexed {len(updated)} packages')


if __name__ == '__main__':
    main()
