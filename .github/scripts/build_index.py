# -*- coding: utf-8 -*-
"""Build the APT Packages index from debs/.
Shared by .github/workflows/release.yml (commits the result back to the repo)
and .github/workflows/deploy-pages.yml (deploys the freshly built site).
"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CLEAN_NAME = re.compile(r'[（(]\s*免授权\s*[)）]')
ICON_BASE = 'https://ynyy.xbcxbc.cn/icons/'
DEFAULT_ICON = 'https://ynyy.xbcxbc.cn/CydiaIcon.png'
META_FILE = ROOT / 'repo-meta.json'
def load_meta():
    """完整读取管理台写入的元数据（plugins 覆盖 + 全局设置）。"""
    if not META_FILE.exists():
        return {}
    try:
        return json.loads(META_FILE.read_text(encoding='utf-8'))
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
def write_release():
    """重建 Release 的校验字段；头部（Origin/Label/Architectures 等）原样保留。"""
    release_file = ROOT / 'Release'
    header = []
    if release_file.is_file():
        for line in release_file.read_text(encoding='utf-8').splitlines():
            if line.rstrip() in ('MD5Sum:', 'SHA1:', 'SHA256:'):
                break
            header.append(line)
    def hash_section(title, algo):
        lines = [title + ':']
        for name in ('Packages', 'Packages.bz2', 'Packages.xz', 'Packages.zst'):
            f = ROOT / name
            if f.is_file():
                digest = hashlib.new(algo, f.read_bytes()).hexdigest()
                lines.append(' %s %d %s' % (digest, f.stat().st_size, name))
        return lines
    NL = chr(10)
    text = (NL.join(header).rstrip(NL) + NL + NL
            + NL.join(hash_section('MD5Sum', 'md5')) + NL + NL
            + NL.join(hash_section('SHA1', 'sha1')) + NL + NL
            + NL.join(hash_section('SHA256', 'sha256')) + NL)
    release_file.write_text(text, encoding='utf-8')
def main():
    meta = load_meta()
    overrides = meta.get('plugins', {}) or {}
    # 源图标内容哈希，用于单插件「使用源图标」和默认兜底图标的版本号
    source_icon_file = ROOT / 'CydiaIcon.png'
    source_v = icon_version(source_icon_file) if source_icon_file.is_file() else '0'
    # 固定 UTF-8 locale：runner 的 LANG=C 会让中文控制字段出现非 UTF-8 字节
    scan_env = dict(os.environ, LC_ALL='C.UTF-8', LANG='C.UTF-8')
    result = subprocess.run(
        ['dpkg-scanpackages', '-m', 'debs', '/dev/null'],
        cwd=ROOT, capture_output=True, check=False, env=scan_env,
        encoding='utf-8', errors='replace',
    )
    if result.returncode != 0:
        # 单个损坏的 deb 只会被跳过，不再导致整个部署失败
        print('dpkg-scanpackages reported issues (bad debs are skipped):')
        print(result.stderr.strip()[:2000])
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
        filename = next(
            (line.split(':', 1)[1].strip().lstrip('./') for line in lines
             if line.startswith('Filename: ')),
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
                lines.append(f'Icon: {DEFAULT_ICON}?v={source_v}')
        if package == 'iya.banana.ios27animation':
            lines = [
                'Section: 公益插件' if line.startswith('Section: ') else line
                for line in lines
            ]
            lines = [
                'Description: 于你有源 / 已公益' if line.startswith('Description: ') else line
                for line in lines
            ]
            if not any(line.startswith('Section: ') for line in lines):
                lines.append('Section: 公益插件')
        # deb control 里没写 Section 时补一个中性分类，避免 sileo 里出现空白分组
        if not any(line.startswith('Section: ') for line in lines):
            lines.append('Section: 未分类')
        # 索引必须带 Architecture：管理台的架构兼容过滤（与 Sileo 同规则）依赖这个字段。
        # dpkg-scanpackages 正常会输出；缺失时按当前主流 64 位补齐，避免整包被误判为不可安装。
        if not any(line.startswith('Architecture: ') for line in lines):
            lines.append('Architecture: iphoneos-arm64')
        # 管理台写入的显示名 / 描述 / 分类覆盖：
        # 路径键保存「同包名多变体」各自的设置，包名键是用户在管理台编辑后的最新意图。
        # 两者合并，同名字段由包名键覆盖——否则编辑分类/作者会被旧的路径键永久盖住。
        ov = {}
        if filename:
            ov = dict(find_override(overrides, filename) or {})
        name_ov = find_override(overrides, package) or {}
        if name_ov:
            ov.update(name_ov)
        if ov.get('name'):
            lines = [line for line in lines if not line.startswith('Name: ')]
            lines.append('Name: ' + str(ov['name']))
        if ov.get('description'):
            lines = [line for line in lines if not line.startswith('Description: ')]
            lines.append('Description: ' + str(ov['description']))
        if ov.get('author'):
            lines = [line for line in lines if not line.startswith('Author: ')]
            lines.append('Author: ' + str(ov['author']))
        # 管理台里把插件移动到某个分类目录后，源索引的 Section 同步改成该分类
        if ov.get('section'):
            lines = [line for line in lines if not line.startswith('Section: ')]
            lines.append('Section: ' + str(ov['section']))

        # 拉取时本地化的详情页（含截图）：Sileo 详情页直接显示截图
        if ov.get('depiction'):
            dep_url = str(ov['depiction'])
            # 版本参数取 depiction 内容哈希：内容一变，Sileo 就会重新拉取详情页
            dep_name = dep_url.rsplit('/', 1)[-1].split('?')[0]
            dep_file = ROOT / 'depictions' / dep_name
            if dep_file.is_file():
                dep_url += '?v=' + hashlib.sha256(dep_file.read_bytes()).hexdigest()[:8]
            # 大小写不敏感清除：deb control 里可能带 Sileodepiction / Depiction 变体，
            # 不清干净会和本源详情页并存，Sileo 读了外源那行
            lines = [line for line in lines
                     if not line.lower().startswith(('sileodepiction: ', 'depiction: '))]
            lines.append('SileoDepiction: ' + dep_url)
        # 单插件「使用源图标」：只把这一个插件的 Icon 换成源图标
        if ov.get('icon'):
            icon_url = str(ov['icon'])
            if 'CydiaIcon.png' in icon_url:
                icon_url = f'{DEFAULT_ICON}?v={source_v}'
            lines = [line for line in lines if not line.startswith('Icon: ')]
            lines.append('Icon: ' + icon_url)
        updated.append('\n'.join(lines))
    (ROOT / 'Packages').write_text('\n\n'.join(updated) + '\n\n', encoding='utf-8')
    # 主页由仓库直接维护，索引脚本不再改写其中的名称
    write_release()
    print(f'Indexed {len(updated)} packages')
if __name__ == '__main__':
    import sys
    if '--release-only' in sys.argv:
        write_release()
        print('Release hashes refreshed')
    else:
        main()
