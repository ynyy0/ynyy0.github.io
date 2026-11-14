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
BTN_TOKEN = re.compile(r'@@RCBTN\{.*?\}@@')


def strip_btn_tokens(s):
    """纯文本描述里不能出现 @@RCBTN{...}@@ 标记（列表页会原样显示）。
    摘要按 160 字截断时会把标记截成半截（没有结尾的 }@@），
    所以先删完整标记，再从残留的 @@RCBTN 处直接截断到行尾。"""
    t = BTN_TOKEN.sub('', str(s or '')).strip()
    cut = t.find('@@RCBTN')
    if cut >= 0:
        t = t[:cut].strip()
    return t
ICON_BASE = 'https://ynyy.xbcxbc.cn/icons/'
DEFAULT_ICON = 'https://ynyy.xbcxbc.cn/CydiaIcon.png'
META_FILE = ROOT / 'repo-meta.json'
SITE_BASE = ICON_BASE[:-len('/icons/')] if ICON_BASE.endswith('/icons/') else ICON_BASE

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


def image_size(path):
    """读取图片像素尺寸（PNG/GIF/JPEG），失败返回 None。
    DepictionImageView 必须给 width/height，所以这里从仓库里的实际文件读。"""
    try:
        with open(path, 'rb') as f:
            head = f.read(26)
        if head[:8] == b'\x89PNG\r\n\x1a\n':
            return (int.from_bytes(head[16:20], 'big'),
                    int.from_bytes(head[20:24], 'big'))
        if head[:6] in (b'GIF87a', b'GIF89a'):
            return (int.from_bytes(head[6:8], 'little'),
                    int.from_bytes(head[8:10], 'little'))
        data = path.read_bytes()
    except Exception:
        return None
    n = len(data)
    if n < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    i = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
           0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], 'big')
        if m in sof:
            h = int.from_bytes(data[i + 5:i + 7], 'big')
            w = int.from_bytes(data[i + 7:i + 9], 'big')
            return (w, h)
        i += 2 + seg
    return None


def sileo_text(s):
    """把 App 面板的纯文本行转成 Sileo DepictionMarkdownView 能等价显示的 Markdown。
    App 里一个回车就是换行；Markdown 里单换行会被合并成空格，所以行尾补两个空格
    （CommonMark 硬换行）。行首 '* ' 在 Markdown 里会变成无序列表符号，转义成字面星号。"""
    out = []
    for line in (s or '').split('\n'):
        line = line.rstrip()
        if line.startswith('* '):
            line = '\\* ' + line[2:]
        out.append(line + '  ' if line else '')
    return '\n'.join(out).rstrip()


def desc_views(md):
    """把含 ![alt](url) 的 Markdown 拆成 Sileo 视图列表：
    正文 -> DepictionMarkdownView；图片 -> 统一按 {370,456} 的卡片尺寸显示。
    单图用 DepictionImageView（同样 370×456），两张及以上用 DepictionScreenshotsView
    横向从左到右排列，不再上下堆叠。
    Sileo 的 DepictionMarkdownView 不渲染图片，也没有 DepictionScreenshotView 这个类。"""
    segs = re.split(r'(!\[[^\]]*\]\([^)]*\)|@@RCBTN\{.*?\}@@)', md or '')
    parts = []  # [(kind, value)]，kind = 'text' | 'img' | 'btn'
    for seg in segs:
        seg = seg.strip('\n').strip()
        if not seg:
            continue
        bm = re.match(r'@@RCBTN(\{.*\})@@$', seg, re.S)
        if bm:
            parts.append(('btn', bm.group(1)))
            continue
        m = re.match(r'!\[([^\]]*)\]\(([^)]+)\)', seg)
        if m:
            url = m.group(2).strip()
            if url:
                parts.append(('img', url))
        else:
            parts.append(('text', seg))
    imgs = [v for k, v in parts if k == 'img']

    # 统一卡片尺寸：与拉取的 Hello Folder 详情页一致（itemSize {370,456}，圆角 14）
    CARD_W, CARD_H, CARD_R = 370, 456, 14

    def one_image(url):
        return {"class": "DepictionImageView",
                "URL": url, "url": url,
                "width": CARD_W, "height": CARD_H, "cornerRadius": CARD_R}

    carousel = {"class": "DepictionScreenshotsView",
                "itemCornerRadius": CARD_R,
                "itemSize": "{%d, %d}" % (CARD_W, CARD_H),
                "screenshots": [{"url": u, "accessibilityText": "配图"}
                                for u in imgs]}

    views = []
    placed = False
    for kind, val in parts:
        if kind == 'text':
            views.append({"class": "DepictionMarkdownView",
                          "markdown": sileo_text(val), "useSpacing": True})
        elif kind == 'btn':
            try:
                sp = json.loads(val)
            except Exception:
                continue
            img_rel = str(sp.get('img') or '')
            link = str(sp.get('url') or '').strip()
            h = int(sp.get('h', 96) or 96)
            if not img_rel:
                # 没有按钮图（多见于从源详情页还原的按钮）：直接用原生纯色按钮
                txt = str(sp.get('text') or '按钮')
                tcolor = str(sp.get('textColor') or '')
                btn = {
                    "class": "DepictionButtonView",
                    "tintColor": str(sp.get('color') or '#6366F1'),
                    "yPadding": 6,
                    "rcButton": val,
                }
                if link:
                    btn["action"] = link
                    btn["openExternal"] = True
                if tcolor:
                    # Sileo 原生按钮的文字色只能靠内嵌 DepictionLabelView 指定
                    btn["view"] = {
                        "class": "DepictionLabelView", "text": txt,
                        "fontSize": 14, "fontWeight": "medium",
                        "textColor": tcolor, "alignment": 1,
                        "useMargins": False, "usePadding": False,
                    }
                else:
                    btn["text"] = txt
                views.append(btn)
                continue
            img_url = SITE_BASE + '/' + img_rel
            if link:
                # 带链接：用原生按钮包住按钮图（图片本身不透明，整宽 402）
                views.append({
                    "class": "DepictionButtonView",
                    "action": link,
                    "openExternal": True,
                    "tintColor": "#FFFFFF",
                    "yPadding": 6,
                    "rcButton": val,
                    "view": {"class": "DepictionImageView", "URL": img_url,
                             "width": 402, "height": h, "cornerRadius": 0},
                })
            else:
                # Sileo 的 DepictionImageView 默认 alignment=0 / xPadding=0（左对齐），
                # 独立按钮图会从视图左边缘起画，比带内边距的 MarkdownView 正文左移约 20pt；
                # 它又是 scaleAspectFill + clipsToBounds，按钮 PNG 自带圆角，
                # 渲染框宽高比一变就会把左右圆角裁掉（「边缘缺少」）。
                # 收窄到 390 并居中（430pt 屏左右各 20pt），高度按同一比例缩放，保证不裁角。
                bw = 390
                bh = round(h * bw / 402, 2)
                views.append({"class": "DepictionImageView",
                              "URL": img_url, "url": img_url,
                              "width": bw, "height": bh,
                              "cornerRadius": 0, "alignment": 1,
                              "rcButton": val})
        elif len(imgs) >= 2:
            if not placed:
                views.append(carousel)
                placed = True
        else:
            views.append(one_image(val))
    if not views:
        views.append({"class": "DepictionMarkdownView",
                      "markdown": sileo_text(md or ''), "useSpacing": True})
    # 连续的小按钮（长度 <=160）合并成横向一行，每行最多 3 个，与 App 面板一致。
    # 注意要在插 spacer 之前合并，否则连续的按钮会被 spacer 隔开。
    def btn_w(v):
        if not isinstance(v, dict) or not v.get('rcButton'):
            return None
        try:
            return float((json.loads(v['rcButton']) or {}).get('w') or 402)
        except Exception:
            return None

    # 与 App 面板一致：按按钮 spec.w 累计宽度分组，放得下就同一行，
    # 放不下自动换行，不再写死一行 3 个。
    avail = 394.0        # 430pt 屏 - 36pt 左右边距
    merged = []
    k = 0
    while k < len(views):
        w0 = btn_w(views[k])
        if w0 is None or w0 > 160:
            merged.append(views[k]); k += 1; continue
        group = []
        used = 0.0
        while k < len(views):
            w = btn_w(views[k])
            if w is None or w > 160:
                break
            # 与 App 面板一致：窄按钮按 120 计，Sileo 里 3 个一行在重新生成后仍是 3 个
            need = min(120.0, max(80.0, float(w))) + (8.0 if group else 0.0)
            if group and used + need > avail:
                break
            group.append(views[k]); k += 1; used += need
        if len(group) > 1:
            merged.append({"class": "DepictionStackView",
                           "orientation": "landscape",
                           "views": group})
        else:
            merged.extend(group)
    views = merged

    # DepictionTabView 的内容从 y=40 起画，StackView 子视图紧挨排列，没有顶部内边距。
    # 统一按 App 面板 body.spacing = 12pt：页签与第一个块、块与块之间都是 12pt，
    # 只加一层不叠加，否则按钮会离页签太远。spacer 必须带 spacing，Sileo 才真正创建。
    spaced = []
    for v in views:
        spaced.append({"class": "DepictionSpacerView", "spacing": 12})
        spaced.append(v)
    return spaced

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
            lines.append('Description: ' + strip_btn_tokens(ov['description']))
        if ov.get('author'):
            lines = [line for line in lines if not line.startswith('Author: ')]
            lines.append('Author: ' + str(ov['author']))
        # 管理台里把插件移动到某个分类目录后，源索引的 Section 同步改成该分类
        if ov.get('section'):
            lines = [line for line in lines if not line.startswith('Section: ')]
            lines.append('Section: ' + str(ov['section']))

        # 兜底：早期版本把 @@RCBTN{...}@@ 按钮标记写进了纯文本摘要，索引里可能还残留一行。
        # 重建时统一清掉；清空后就是空描述，直接删掉该行，别让列表页显示一段 JSON。
        polished = []
        for line in lines:
            if line.startswith('Description: '):
                t = strip_btn_tokens(line[len('Description: '):])
                if not t:
                    continue
                line = 'Description: ' + t
            polished.append(line)
        lines = polished

        # 拉取时本地化的详情页（含截图）：Sileo 详情页直接显示截图
        # 本地详情页：不存在时自动生成（白菜背景头图 + 描述），任何方式进来的包全覆盖。
        # meta 里有 depiction（App 拉取的截图版）时使用已有文件，不覆盖。
        dep_name = package + '.json'
        dep_dir = ROOT / 'depictions'
        dep_dir.mkdir(exist_ok=True)
        dep_file = dep_dir / dep_name
        # App 改过的描述（富文本优先，其次普通描述）必须同步进详情页正文：
        # 列表页读 Packages 的 Description，详情页读这里的 markdown，是两份副本。
        rich = str(ov.get('rich_description') or '')
        plain = str(ov.get('description') or '')
        # 只有 App 里明确改过描述的包才同步详情页正文；
        # 否则 desc_line 会回退成索引里的一行摘要，把原有长介绍覆盖掉。
        override_desc = rich or plain
        if override_desc:
            override_desc = override_desc.replace('__RC_SITE__', SITE_BASE)
        desc_line = override_desc or next((l.split(':', 1)[1].strip() for l in lines
                                           if l.lower().startswith('description: ')), '')
        if dep_file.is_file():
            try:
                cur = json.loads(dep_file.read_text(encoding='utf-8'))
                changed = False
                for ti, tab in enumerate(cur.get('tabs', [])):
                    # 源详情页的页签名五花八门（详情/描述/插件简介…），
                    # 一律认「第一个页签 + 常见名」，否则用户的排序永远写不进去
                    if ti == 0 or tab.get('tabname') in ('详情', '描述', '插件简介'):
                        if tab['tabname'] != '插件简介':
                            tab['tabname'] = '插件简介'
                            changed = True
                        if override_desc:
                            new_views = desc_views(desc_line) + \
                                [{"class": "DepictionSpacerView", "spacing": 12},
                                 {"class": "DepictionSeparatorView"}]
                            if tab.get('views') != new_views:
                                tab['views'] = new_views
                                changed = True
                # 面板里增删/改名/改色后的页签结构：整份替换 tabs。
                # Sileo 的 DepictionTabControl 高亮色由根 tintColor 控制（全局），
                # 逐页签不同颜色原生不支持，这里取第一个自定义色作为整页高亮色。
                tabs_override = ov.get('panel_tabs') or []
                if isinstance(tabs_override, list) and tabs_override:
                    new_tabs = []
                    for j, t in enumerate(tabs_override):
                        title = str(t.get('title') or ('插件简介' if j == 0 else '第%d页' % (j + 1)))
                        content = str(t.get('content') or '').replace('__RC_SITE__', SITE_BASE)
                        new_tabs.append({
                            "class": "DepictionStackView",
                            "tabname": title,
                            "views": desc_views(content or name) + [
                                {"class": "DepictionSpacerView", "spacing": 12},
                                {"class": "DepictionSeparatorView"}]
                        })
                    if cur.get('tabs') != new_tabs:
                        cur['tabs'] = new_tabs
                        changed = True
                    for t in tabs_override:
                        c = str(t.get('color') or '')
                        if c:
                            if cur.get('tintColor') != c:
                                cur['tintColor'] = c
                                changed = True
                            break
                if changed:
                    dep_file.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception:
                pass
        if not dep_file.is_file():
            bg = SITE_BASE + '/depiction-bg.jpg' if (ROOT / 'depiction-bg.jpg').is_file() \
                else ICON_BASE + 'CydiaIcon.png'
            auto = {
                "class": "DepictionTabView",
                "headerImage": bg,
                "minVersion": "0.3",
                "tabs": [{
                    "class": "DepictionStackView",
                    "tabname": "详情",
                    "views": desc_views(desc_line or name) + [
                        {"class": "DepictionSpacerView", "spacing": 12},
                        {"class": "DepictionSeparatorView"}
                    ]
                }, {
                    "class": "DepictionStackView",
                    "tabname": "更新日志",
                    "views": [
                        {"class": "DepictionMarkdownView", "markdown": "暂无更新日志",
                         "useSpacing": True},
                        {"class": "DepictionSpacerView", "spacing": 12},
                        {"class": "DepictionSeparatorView"}
                    ]
                }]
            }
            dep_file.write_text(json.dumps(auto, ensure_ascii=False, indent=2), encoding='utf-8')
        # 版本参数 = 内容哈希 + 全局修订号：修订号变了所有包的详情页地址都变，
        # Sileo 即使按 URL 缓存也必须重新拉取（一次性解决历史缓存残留）
        dep_url = SITE_BASE + '/depictions/' + dep_name + '?v=' + \
            hashlib.sha256(dep_file.read_bytes()).hexdigest()[:8] + 'r2'
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
    if len(updated) < 5:
        # 重建结果异常（坏 deb 把扫描打挂/目录为空）：拒绝写空索引，
        # 保留上一版索引——否则整源插件名变包名、分组全乱
        print('ERROR: only %d packages parsed, refusing to overwrite the index' % len(updated))
        raise SystemExit(1)
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
