from flask import Flask, render_template, request, jsonify, send_file, session, redirect
import asyncio
import re
import requests
import json
import os
import base64
from datetime import datetime
from crawl4ai import AsyncWebCrawler
import secrets
import io
from urllib.parse import urlparse, quote
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from functools import lru_cache

app = Flask(__name__, static_url_path='/aizaobao/static')
# 从环境变量读取secret key，如果没有则生成一个临时的
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(16)

# 配置应用根路径，确保 URL 生成正确
app.config['APPLICATION_ROOT'] = '/aizaobao'

# 默认配置
DEFAULT_CONFIG = {
    'group_id': '',
    'api_key': '',
    'model': 'speech-2.5-hd-preview',
    'voice_id': 'female-shaonv',
    'speed': 1.0,
    'pitch': 0,
    'vol': 1.0,
    'emotion': 'neutral',
    'sample_rate': 32000,
    'bitrate': 128000,
    'format': 'mp3',
    'weather_location': '上海'
}

# 简易天气缓存（内存级，重启失效）
_weather_cache = {
    'key': None,
    'data': None,
    'ts': None
}

# 航运新闻来源（可按需增删）
SHIPPING_SOURCES = [
    {"name": "Splash 247", "url": "https://splash247.com/"},
    {"name": "Ship & Bunker", "url": "https://shipandbunker.com/news/world"},
    {"name": "信德海事网", "url": "https://xindemarinenews.com/"},
    {"name": "gCaptain", "url": "https://gcaptain.com/"},
]

# 已知来源的RSS兜底
SOURCE_FEEDS = {
    'gcaptain.com': 'https://gcaptain.com/feed/',
    # 可按需扩展更多RSS
}

# 非新闻类关键词黑名单（出现则过滤）
EXCLUDE_KEYWORDS = {
    'facebook', 'x', 'twitter', 'linkedin', 'youtube', 'apple', 'google play', 'rss', 'subscribe', 'follow',
    'jobs', 'login', 'sign in', 'sign up', '注册', '登录', '关于我们', 'contact', '联系我们', 'privacy', 'cookie', 'terms',
    'advertise', 'advertising', '广告', '专题', '导航', 'menu', 'newsletter', 'get daily email', 'archive', 'news archive',
    '版权', 'copyright', 'homepage', 'home', 'sector', 'region', 'categories', 'category', 'search', 'recent',
    'publications', 'magazines', 'events', 'splash extra', 'news & features', 'bunker prices', 'prices', 'compliance costs'
}

NAV_SITE_NAMES = {
    'splash247', 'splash 247', 'ship & bunker', 'ship and bunker', 'gcaptain', '信德海事网', 'xinde marine', 'xindemarinenews'
}

# 城市到附近海域的简易映射
MARINE_AREA_MAP = {
    '天津': '渤海湾',
    '塘沽': '渤海湾',
    '大连': '渤海',
    '青岛': '黄海',
    '烟台': '渤海',
    '威海': '黄海',
    '上海': '东海',
    '宁波': '东海',
    '舟山': '东海',
    '连云港': '黄海',
    '厦门': '台湾海峡',
    '泉州': '台湾海峡',
    '福州': '台湾海峡',
    '广州': '南海',
    '深圳': '南海',
    '湛江': '南海',
    '海口': '南海',
    '三亚': '南海',
}

MARINE_ALIAS = {
    '天津': {'query': 'Bohai Sea', 'name': '渤海湾'},
}

def _beaufort_from_kmph(kmph: float) -> int:
    # 近似换算：m/s ≈ km/h / 3.6, 使用WMO等级
    ms = (kmph or 0) / 3.6
    thresholds = [0.5, 1.5, 3.3, 5.5, 7.9, 10.7, 13.8, 17.1, 20.7, 24.4, 28.4, 32.6]
    for i, t in enumerate(thresholds):
        if ms < t:
            return i
    return 12

def _is_excluded_title(text: str) -> bool:
    t = (text or '').strip()
    tl = t.lower()
    # 图片行或空
    if not t or t.startswith('!['):
        return True
    # 黑名单关键词
    for kw in EXCLUDE_KEYWORDS:
        if kw in tl:
            return True
    # 站点名（纯品牌名）
    if tl in NAV_SITE_NAMES:
        return True
    # 英文标题最小词数/长度限制，过滤导航项
    has_chinese = re.search(r'[\u4e00-\u9fa5]', t) is not None
    words = re.findall(r'[A-Za-z0-9]+', t)
    if not has_chinese:
        if len(words) < 3 and len(t) < 20:
            return True
    # 过短标题
    if len(t) < 8:
        return True
    return False

SOURCE_RULES = {
    'splash247.com': {
        'allow_hosts': {'splash247.com'},
        'exclude_prefixes': ['/category/', '/region/', '/publications/', '/magazines/', '/events/', '/jobs/', '/sector/', '/renewables/', '/offshore/', '/piracy/'],
        'allow_patterns': [re.compile(r'^/[^/]+/?$')],  # 根下一级slug
        'require_url': True,
    },
    'shipandbunker.com': {
        'allow_hosts': {'shipandbunker.com'},
        'exclude_prefixes': ['/prices', '/bi', '/compliance-costs', '/features'],
        'allow_patterns': [
            re.compile(r'^/news/(world|am|emea|asia|ap|asiapacific)/\d'),
            re.compile(r'^/news/(world|am|emea|asia|ap|asiapacific)/')
        ],
        'require_url': True,
    },
    'xindemarinenews.com': {
        'allow_hosts': {'xindemarinenews.com'},
        'exclude_prefixes': ['/category/', '/tag/', '/about', '/contact', '/login'],
        'allow_patterns': [re.compile(r'^/[^/]+/\d'), re.compile(r'^/[^/]+/')],
        'require_url': True,
    },
    'gcaptain.com': {
        'allow_hosts': {'gcaptain.com'},
        'exclude_prefixes': ['/about', '/contact', '/jobs', '/advertise', '/privacy', '/category/', '/tag/'],
        'allow_patterns': [re.compile(r'^/[^/]+/?$')],
        'require_url': True,
    }
}

def get_user_config():
    """获取用户配置"""
    config = session.get('config', DEFAULT_CONFIG.copy())
    return config

def update_user_config(new_config):
    """更新用户配置"""
    config = get_user_config()
    config.update(new_config)
    session['config'] = config
    return config


def _normalize_headline(text: str) -> str:
    """归一化标题用于去重"""
    if not text:
        return ''
    # 去除标点和多余空白，转小写
    cleaned = re.sub(r'[\[\]\(\)（）【】“”"\'\'’‘《》<>:：,，.;。!！?？•·\-—–]+', ' ', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().lower()
    return cleaned


def _extract_headlines(markdown_content: str, max_items: int = 10) -> list:
    """从通用Markdown提取新闻标题"""
    if not markdown_content:
        return []
    lines = markdown_content.splitlines()
    headlines = []
    seen = set()

    patterns = [
        re.compile(r'^\s*[\-*\+]\s+(.+)$'),            # 无序列表
        re.compile(r'^\s*\d+[\.|、]\s*(.+)$'),         # 有序列表
        re.compile(r'^\s*#{1,3}\s+(.+)$'),               # 1-3级标题
        re.compile(r'^\s*\[(.+?)\]\([^\)]+\)\s*$')  # 纯链接行
    ]

    for raw in lines:
        text = raw.strip()
        candidate = None
        for pat in patterns:
            m = pat.match(text)
            if m:
                candidate = m.group(1).strip()
                break
        if not candidate:
            m = re.search(r'\[(.+?)\]\([^\)]+\)', text)
            if m:
                candidate = m.group(1).strip()
        if not candidate:
            continue
        if _is_excluded_title(candidate):
            continue
        key = _normalize_headline(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        headlines.append(candidate)
        if len(headlines) >= max_items:
            break
    return headlines


def _extract_headlines_for_source(markdown_content: str, source_url: str, max_items: int = 10) -> list:
    """基于来源域名的链接级提取与过滤，返回更干净的新闻标题列表"""
    if not markdown_content:
        return []
    try:
        src_host = urlparse(source_url).hostname or ''
        src_host = src_host.lower()
        rules = SOURCE_RULES.get(src_host)
        if not rules:
            # 未定义规则时退回通用提取
            return _extract_headlines(markdown_content, max_items=max_items)

        lines = markdown_content.splitlines()
        headlines: list[dict] = []
        seen = set()
        link_re = re.compile(r'\[([^\]]+?)\]\((https?://[^\)]+)\)')

        def allow_by_url(u: str) -> bool:
            try:
                p = urlparse(u)
                host = (p.hostname or '').lower()
                path = p.path or '/'
                if host not in rules['allow_hosts']:
                    return False
                for pref in rules['exclude_prefixes']:
                    if path.startswith(pref):
                        return False
                if 'allow_patterns' in rules and rules['allow_patterns']:
                    ok = any(pat.search(path) for pat in rules['allow_patterns'])
                    if not ok:
                        return False
                return True
            except Exception:
                return False

        for raw in lines:
            text = raw.strip()
            # 忽略图片/空行
            if not text or text.startswith('!['):
                continue
            m = link_re.search(text)
            if not m:
                # 对中文来源，可补充通用提取作兜底
                continue
            title, url = m.group(1).strip(), m.group(2).strip()
            if _is_excluded_title(title):
                continue
            if not allow_by_url(url):
                continue
            key = _normalize_headline(title)
            if not key or key in seen:
                continue
            seen.add(key)
            headlines.append({'title': title, 'url': url})
            if len(headlines) >= max_items:
                break
        # 若严格规则结果太少，回退通用抽取补齐
        if len(headlines) < max_items // 2:
            fallback = _extract_headlines(markdown_content, max_items=max_items)
            for t in fallback:
                k = _normalize_headline(t)
                if k and k not in seen:
                    seen.add(k)
                    headlines.append({'title': t, 'url': ''})
                    if len(headlines) >= max_items:
                        break
        return headlines[:max_items]
    except Exception:
        return _extract_headlines(markdown_content, max_items=max_items)

def _fallback_extract_source(source_url: str, max_items: int = 8) -> list:
    """使用 requests + BeautifulSoup 的后备解析，按来源规则提取标题"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
        }
        resp = requests.get(source_url, headers=headers, timeout=12)
        if resp.status_code != 200:
            return []
        html = resp.text
        soup = BeautifulSoup(html, 'html.parser')
        rules = SOURCE_RULES.get((urlparse(source_url).hostname or '').lower())
        if not rules:
            # 通用：抓取可见a标签文本
            texts = []
            seen = set()
            for a in soup.select('a'):
                title = (a.get_text() or '').strip()
                href = a.get('href') or ''
                if not href.startswith('http'):
                    # 拼绝对URL
                    try:
                        p = urlparse(source_url)
                        base = f"{p.scheme}://{p.netloc}"
                        href = base + href if href.startswith('/') else href
                    except Exception:
                        pass
                if _is_excluded_title(title):
                    continue
                key = _normalize_headline(title)
                if key and key not in seen:
                    seen.add(key)
                    texts.append({'title': title, 'url': href})
                if len(texts) >= max_items:
                    break
            return texts
        # 按规则
        result = []
        seen = set()
        for a in soup.select('a'):
            title = (a.get_text() or '').strip()
            url = a.get('href') or ''
            if not url:
                continue
            if not url.startswith('http'):
                try:
                    p = urlparse(source_url)
                    base = f"{p.scheme}://{p.netloc}"
                    url = base + url if url.startswith('/') else url
                except Exception:
                    continue
            if _is_excluded_title(title):
                continue
            # URL过滤
            def allow_by_url(u: str) -> bool:
                try:
                    pu = urlparse(u)
                    host = (pu.hostname or '').lower()
                    path = pu.path or '/'
                    if host not in rules['allow_hosts']:
                        return False
                    for pref in rules['exclude_prefixes']:
                        if path.startswith(pref):
                            return False
                    if 'allow_patterns' in rules and rules['allow_patterns']:
                        ok = any(pat.search(path) for pat in rules['allow_patterns'])
                        if not ok:
                            return False
                    return True
                except Exception:
                    return False
            if not allow_by_url(url):
                continue
            key = _normalize_headline(title)
            if key and key not in seen:
                seen.add(key)
                result.append({'title': title, 'url': url})
            if len(result) >= max_items:
                break
        return result
    except Exception:
        return []

def _rss_fallback_titles(hostname: str, max_items: int = 10) -> list:
    feed_url = SOURCE_FEEDS.get(hostname)
    if not feed_url:
        return []
    try:
        resp = requests.get(feed_url, timeout=10)
        if resp.status_code != 200:
            return []
        results = []
        root = ET.fromstring(resp.text)
        for item in root.iter('item'):
            title_el = item.find('title')
            link_el = item.find('link')
            if title_el is None or not title_el.text or link_el is None or not link_el.text:
                continue
            title = title_el.text.strip()
            url = link_el.text.strip()
            if title and not _is_excluded_title(title):
                results.append({'title': title, 'url': url})
            if len(results) >= max_items:
                break
        return results
    except Exception:
        return []

@lru_cache(maxsize=256)
def _translate_to_zh(text: str) -> str:
    """尝试将英文标题翻译为中文；失败则返回原文。使用 mymemory 免费接口做轻量翻译。"""
    try:
        if not text or re.search(r'[\u4e00-\u9fa5]', text):
            return text
        # 简单双词不翻译
        if len(text.split()) <= 2:
            return text
        resp = requests.get(
            'https://api.mymemory.translated.net/get',
            params={'q': text, 'langpair': 'en|zh-CN'}, timeout=8
        )
        if resp.status_code == 200:
            data = resp.json()
            translated = data.get('responseData', {}).get('translatedText')
            if translated:
                return translated
        return text
    except Exception:
        return text

def format_news(markdown_or_items):
    """
    将抓取的内容（Markdown或标题列表）格式化为航运早报
    返回: formatted_output, news_items (<=10), date_str
    """
    # 统一为标题列表
    if isinstance(markdown_or_items, str):
        items = _extract_headlines(markdown_or_items, max_items=10)
    else:
        items = list(markdown_or_items or [])[:10]

    date_str = datetime.now().strftime("%Y年%m月%d日")
    
    formatted_output = f"{date_str} 航运早报\n\n"
    news_items_plain = []
    for i, item in enumerate(items[:10], 1):
        if isinstance(item, dict):
            title = item.get('title') or ''
            url = item.get('url') or ''
            src = item.get('source') or ''
            news_items_plain.append(title)
            if url:
                formatted_output += f"{i}、<a href=\"{url}\" target=\"_blank\" rel=\"noopener\">{title}</a>（来源：{src}）\n\n"
            else:
                formatted_output += f"{i}、{title}（来源：{src}）\n\n"
        else:
            text = str(item)
            news_items_plain.append(text)
            formatted_output += f"{i}、{text}\n\n"
    formatted_output = formatted_output.rstrip('\n')
    
    return formatted_output, news_items_plain[:10], date_str

def remove_newlines(text):
    """
    去除文本中的换行符，用于API调用
    """
    return text.replace('\n', '').replace('\r', '')

def create_cache_folder():
    """创建缓存文件夹"""
    cache_folder = "cache"
    if not os.path.exists(cache_folder):
        os.makedirs(cache_folder)
    return cache_folder

def get_cache_file_path():
    """获取当天的缓存文件路径"""
    cache_folder = create_cache_folder()
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(cache_folder, f"news_{today}.json")

def save_news_cache(formatted_news, news_items, date_str):
    """保存新闻缓存"""
    try:
        cache_file = get_cache_file_path()
        cache_data = {
            'formatted_news': formatted_news,
            'news_items': news_items,
            'date_str': date_str,
            'cached_time': datetime.now().isoformat(),
            'cache_date': datetime.now().strftime("%Y-%m-%d")
        }
        
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        
        print(f"新闻缓存已保存: {cache_file}")
        return True
    except Exception as e:
        print(f"保存缓存失败: {e}")
        return False

def load_news_cache():
    """加载新闻缓存"""
    try:
        cache_file = get_cache_file_path()
        
        # 检查缓存文件是否存在
        if not os.path.exists(cache_file):
            print("缓存文件不存在")
            return None, None, None
        
        # 检查缓存文件是否为今天的
        today = datetime.now().strftime("%Y-%m-%d")
        
        with open(cache_file, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
        
        # 验证缓存日期
        if cache_data.get('cache_date') != today:
            print("缓存已过期")
            return None, None, None
        
        print(f"从缓存加载新闻: {cache_file}")
        return cache_data['formatted_news'], cache_data['news_items'], cache_data['date_str']
        
    except Exception as e:
        print(f"加载缓存失败: {e}")
        return None, None, None

def clear_old_cache():
    """清理旧的缓存文件（保留最近3天）"""
    try:
        cache_folder = create_cache_folder()
        current_time = datetime.now()
        
        for filename in os.listdir(cache_folder):
            if filename.startswith('news_') and filename.endswith('.json'):
                file_path = os.path.join(cache_folder, filename)
                # 获取文件修改时间
                file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                # 如果文件超过3天，删除它
                if (current_time - file_time).days > 3:
                    os.remove(file_path)
                    print(f"删除旧缓存文件: {filename}")
    except Exception as e:
        print(f"清理缓存失败: {e}")

# 新闻优先级关键词
PRIORITY_KEYWORDS_LEVEL1 = [
    '天津', '天津港', '渤海', '渤海湾', '环渤海', '滨海新区',
    '唐山', '曹妃甸', '秦皇岛', '黄骅', '曹妃甸港', '秦皇岛港',
    '大连', '营口', '锦州', '青岛', '烟台', '日照'
]
PRIORITY_KEYWORDS_LEVEL2 = [
    '中国', '国内', '港口', '码头', '航道', '疏浚', '北方港',
    '上港', '宁波舟山', '厦门港', '深圳港', '广州港', '连云港', '福州港', '海关', '铁路集疏运'
]

async def get_news_content():
    """获取航运新闻内容（带缓存机制，多源聚合）"""
    # 先尝试从缓存加载
    cached_news, cached_items, cached_date = load_news_cache()

    if cached_news is not None:
        print("使用缓存的新闻内容")
        return cached_news, cached_items, cached_date

    print("从网络获取最新航运新闻")
    try:
        async with AsyncWebCrawler() as crawler:
            tasks = [crawler.arun(url=src["url"], bypass_cache=True) for src in SHIPPING_SOURCES]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            collected = []  # 收集原始项用于打分排序
            seen = set()

            for src, res in zip(SHIPPING_SOURCES, results):
                md = ''
                if not isinstance(res, Exception) and res is not None:
                    md = getattr(res, 'markdown', '') or ''
                host = (urlparse(src['url']).hostname or '').lower()

                per_source = _extract_headlines_for_source(md, src['url'], max_items=12) if md else []
                if len(per_source) < 2:
                    rss_titles = _rss_fallback_titles(host, max_items=12)
                    if rss_titles:
                        per_source = rss_titles
                if not per_source:
                    per_source = _fallback_extract_source(src['url'], max_items=12)

                for item in per_source:
                    title_raw = item['title'] if isinstance(item, dict) else str(item)
                    url = item.get('url') if isinstance(item, dict) else ''
                    title_cn = _translate_to_zh(title_raw)
                    key = _normalize_headline(title_cn)
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    collected.append({'title': title_cn, 'url': url, 'source': src['name']})
                    # 上限收集 40 条用于排序
                    if len(collected) >= 40:
                        break
                if len(collected) >= 40:
                    break

            if not collected:
                date_str = datetime.now().strftime("%Y年%m月%d日")
                placeholder = [
                    "今日未获取到航运新闻，请稍后重试或点击刷新。",
                    "如长期无结果，请检查服务器网络与Playwright浏览器安装。"
                ]
                formatted = f"{date_str} 航运早报\n\n" + "\n\n".join([f"{i+1}、{t}" for i, t in enumerate(placeholder)])
                save_news_cache(formatted, placeholder, date_str)
                clear_old_cache()
                return formatted, placeholder, date_str

            # 打分：优先天津及环渤海（L1），其次国内港口（L2）
            def score_item(it: dict) -> int:
                t = it.get('title') or ''
                s = 0
                for kw in PRIORITY_KEYWORDS_LEVEL1:
                    if kw in t:
                        s += 10
                for kw in PRIORITY_KEYWORDS_LEVEL2:
                    if kw in t:
                        s += 3
                return s

            collected.sort(key=score_item, reverse=True)
            top_items = collected[:10]

            formatted_news, news_items, date_str = format_news(top_items)

            save_news_cache(formatted_news, news_items, date_str)
            clear_old_cache()

            return formatted_news, news_items, date_str

    except Exception as e:
        print(f"获取航运新闻失败: {e}")
        return None, None, None

def create_audio_folder():
    """创建音频文件夹"""
    audio_folder = "static/audio"
    if not os.path.exists(audio_folder):
        os.makedirs(audio_folder)
    return audio_folder

def generate_audio(text, config):
    """调用Minimax API生成音频"""
    if not config.get('group_id') or not config.get('api_key'):
        return None, "请先配置Minimax API信息", None
    
    url = f"https://api.minimax.chat/v1/t2a_v2?GroupId={config['group_id']}"
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": config.get('model', 'speech-2.5-hd-preview'),
        "text": text,
        "timber_weights": [
            {
                "voice_id": config.get('voice_id', 'Boyan_new_platform'),
                "weight": 100
            }
        ],
        "voice_setting": {
            "voice_id": "",
            "speed": config.get('speed', 1.0),
            "pitch": config.get('pitch', 0),
            "vol": config.get('vol', 1.0),
            "emotion": config.get('emotion', 'neutral'),
            "latex_read": False
        },
        "audio_setting": {
            "sample_rate": config.get('sample_rate', 32000),
            "bitrate": config.get('bitrate', 128000),
            "format": config.get('format', 'mp3')
        },
        "language_boost": "auto"
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code == 200:
            response_data = response.json()
            
            if response_data.get("base_resp", {}).get("status_code") == 0:
                audio_data = response_data.get("data", {}).get("audio")
                
                if audio_data:
                    try:
                        audio_bytes = bytes.fromhex(audio_data)
                        
                        # 保存音频文件到静态目录，以便分享
                        audio_folder = create_audio_folder()
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        filename = f"shipping_news_{timestamp}.mp3"
                        file_path = os.path.join(audio_folder, filename)
                        
                        with open(file_path, 'wb') as f:
                            f.write(audio_bytes)
                        
                        # 生成分享URL
                        share_url = f"/aizaobao/static/audio/{filename}"
                        
                        return audio_bytes, None, share_url
                    except Exception as e:
                        return None, f"音频数据解析失败: {str(e)}", None
                else:
                    return None, "API返回的音频数据为空", None
            else:
                error_msg = response_data.get("base_resp", {}).get("status_msg", "未知错误")
                return None, f"API调用失败: {error_msg}", None
        else:
            return None, f"HTTP请求失败: {response.status_code}", None
            
    except Exception as e:
        return None, f"请求异常: {str(e)}", None

@app.route('/aizaobao/')
def index():
    """主页"""
    return render_template('index.html')

@app.route('/aizaobao/api/config', methods=['GET', 'POST'])
def config_api():
    """配置API"""
    if request.method == 'GET':
        config = get_user_config()
        # 不返回API密钥的完整信息
        safe_config = config.copy()
        if safe_config.get('api_key'):
            safe_config['api_key'] = safe_config['api_key'][:10] + '...' if len(safe_config['api_key']) > 10 else safe_config['api_key']
        return jsonify(safe_config)
    
    elif request.method == 'POST':
        try:
            new_config = request.json
            config = update_user_config(new_config)
            return jsonify({'success': True, 'message': '配置更新成功'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'配置更新失败: {str(e)}'})

@app.route('/aizaobao/api/news')
def get_news():
    """获取新闻API"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        news_content, news_items, date_str = loop.run_until_complete(get_news_content())
        loop.close()
        
        if news_content:
            cache_file = get_cache_file_path()
            is_cached = os.path.exists(cache_file)
            return jsonify({
                'success': True,
                'content': news_content,
                'items': news_items,
                'date_str': date_str,
                'timestamp': datetime.now().isoformat(),
                'from_cache': is_cached
            })
        else:
            return jsonify({'success': False, 'message': '获取新闻失败（内容为空）'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'获取新闻异常: {str(e)}'})

@app.route('/aizaobao/api/refresh-news', methods=['POST'])
def refresh_news():
    """强制刷新新闻（忽略缓存）"""
    try:
        cache_file = get_cache_file_path()
        if os.path.exists(cache_file):
            os.remove(cache_file)
            print(f"删除缓存文件: {cache_file}")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        news_content, news_items, date_str = loop.run_until_complete(get_news_content())
        loop.close()
        
        if news_content:
            return jsonify({
                'success': True,
                'content': news_content,
                'items': news_items,
                'date_str': date_str,
                'timestamp': datetime.now().isoformat(),
                'from_cache': False,
                'message': '新闻已强制刷新'
            })
        else:
            return jsonify({'success': False, 'message': '强制刷新失败'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'强制刷新异常: {str(e)}'})

@app.route('/aizaobao/api/history')
def get_history():
    """获取历史记录列表"""
    try:
        cache_folder = create_cache_folder()
        history_files = []
        
        # 扫描缓存目录中的所有新闻文件
        for filename in os.listdir(cache_folder):
            if filename.startswith('news_') and filename.endswith('.json'):
                file_path = os.path.join(cache_folder, filename)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        cache_data = json.load(f)
                    
                    history_files.append({
                        'cache_date': cache_data.get('cache_date'),
                        'date_str': cache_data.get('date_str'),
                        'cached_time': cache_data.get('cached_time'),
                        'news_items': cache_data.get('news_items', []),
                        'filename': filename
                    })
                except Exception as e:
                    print(f"读取历史文件失败 {filename}: {e}")
                    continue
        
        # 按日期倒序排列
        history_files.sort(key=lambda x: x['cache_date'], reverse=True)
        
        return jsonify({
            'success': True,
            'history': history_files
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'获取历史记录失败: {str(e)}'})

@app.route('/aizaobao/api/history/<cache_date>')
def get_history_detail(cache_date):
    """获取特定日期的历史记录详情"""
    try:
        cache_folder = create_cache_folder()
        cache_file = os.path.join(cache_folder, f"news_{cache_date}.json")
        
        if not os.path.exists(cache_file):
            return jsonify({'success': False, 'message': '历史记录不存在'})
        
        with open(cache_file, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
        
        return jsonify({
            'success': True,
            'content': cache_data.get('formatted_news'),
            'items': cache_data.get('news_items'),
            'date_str': cache_data.get('date_str'),
            'cached_time': cache_data.get('cached_time'),
            'cache_date': cache_data.get('cache_date')
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'获取历史记录详情失败: {str(e)}'})

@app.route('/aizaobao/api/generate-audio', methods=['POST'])
def generate_audio_api():
    """生成音频API"""
    try:
        data = request.json
        text = data.get('text', '')
        
        if not text:
            return jsonify({'success': False, 'message': '文本内容不能为空'})
        
        config = get_user_config()
        
        # 去除换行符用于API调用
        clean_text = remove_newlines(text)
        
        audio_bytes, error, share_url = generate_audio(clean_text, config)
        
        if audio_bytes:
            # 将音频数据编码为base64
            audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')
            
            return jsonify({
                'success': True,
                'audio_data': audio_base64,
                'filename': f"shipping_news_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3",
                'share_url': share_url
            })
        else:
            return jsonify({'success': False, 'message': error})
            
    except Exception as e:
        return jsonify({'success': False, 'message': f'生成音频异常: {str(e)}'})

@app.route('/aizaobao/api/download-audio', methods=['POST'])
def download_audio():
    """下载音频文件"""
    try:
        data = request.json
        audio_data = data.get('audio_data', '')
        filename = data.get('filename', 'ai_news.mp3')
        
        if not audio_data:
            return jsonify({'success': False, 'message': '音频数据为空'})
        
        # 解码base64音频数据
        audio_bytes = base64.b64decode(audio_data)
        
        # 创建内存文件对象
        audio_file = io.BytesIO(audio_bytes)
        audio_file.seek(0)
        
        return send_file(
            audio_file,
            as_attachment=True,
            download_name=filename,
            mimetype='audio/mpeg'
        )
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'下载失败: {str(e)}'})

@app.route('/aizaobao/static/<path:filename>')
def serve_static_files(filename):
    """处理静态文件请求（音频文件分享）"""
    try:
        # 构建文件路径
        file_path = os.path.join('static', filename)
        
        # 检查文件是否存在
        if not os.path.exists(file_path):
            return jsonify({'error': '文件不存在'}), 404
        
        # 返回文件
        return send_file(
            file_path,
            as_attachment=False,  # 不强制下载，可以在浏览器中播放
            mimetype='audio/mpeg' if filename.endswith('.mp3') else None
        )
        
    except Exception as e:
        return jsonify({'error': f'访问文件失败: {str(e)}'}), 500

@app.route('/aizaobao/api/weather')
def get_weather():
    """根据配置或查询参数返回天气简报（用于头部滚动条）"""
    try:
        loc = request.args.get('location')
        if not loc:
            cfg = get_user_config()
            loc = cfg.get('weather_location') or '上海'
        # wttr.in 免费天气接口
        url = f"https://wttr.in/{loc}?format=j1"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return jsonify({'success': False, 'message': '天气服务不可用'})
        data = r.json()
        # 当前天气
        current = data.get('current_condition', [{}])[0]
        area = data.get('nearest_area', [{}])[0]
        area_name = (area.get('areaName', [{}])[0].get('value') or loc) if isinstance(area.get('areaName', []), list) else loc
        temp_c = current.get('temp_C')
        desc = (current.get('weatherDesc', [{}])[0].get('value')) if isinstance(current.get('weatherDesc', []), list) else ''
        wind_kmph = float(current.get('windspeedKmph') or 0)
        wind_dir = current.get('winddir16Point') or ''
        bft = _beaufort_from_kmph(wind_kmph)
        # 未来2天天气
        weather = data.get('weather', [])
        forecast = []
        for w in weather[:2]:
            date_v = w.get('date')
            maxt = w.get('maxtempC')
            mint = w.get('mintempC')
            forecast.append({'date': date_v, 'maxC': maxt, 'minC': mint})
        # 海区（如天津 => 渤海湾）
        marine = None
        for k, v in MARINE_ALIAS.items():
            if k in loc:
                try:
                    r2 = requests.get(f"https://wttr.in/{v['query']}?format=j1", timeout=10)
                    if r2.status_code == 200:
                        d2 = r2.json()
                        cur2 = d2.get('current_condition', [{}])[0]
                        mk = float(cur2.get('windspeedKmph') or 0)
                        md = cur2.get('winddir16Point') or ''
                        marine = {
                            'name': v['name'],
                            'wind': {
                                'dir': md,
                                'kmph': mk,
                                'bft': _beaufort_from_kmph(mk)
                            }
                        }
                except Exception:
                    marine = None
                break
        return jsonify({
            'success': True,
            'location': area_name,
            'current': {'tempC': temp_c, 'desc': desc, 'wind': {'dir': wind_dir, 'kmph': wind_kmph, 'bft': bft}},
            'forecast': forecast,
            'marine': marine
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'获取天气失败: {str(e)}'})

# 根路径重定向，避免访问 '/' 时 404
@app.route('/')
def root_redirect():
    return redirect('/aizaobao/', code=302)

# 处理浏览器自动请求的 favicon，避免 404 噪音
@app.route('/favicon.ico')
def favicon_handler():
    favicon_path = os.path.join('static', 'favicon.ico')
    if os.path.exists(favicon_path):
        return send_file(favicon_path, mimetype='image/x-icon')
    return ('', 204)

if __name__ == '__main__':
    # 创建templates、static、cache和audio目录
    os.makedirs('templates', exist_ok=True)
    os.makedirs('static', exist_ok=True)
    os.makedirs('static/audio', exist_ok=True)
    os.makedirs('cache', exist_ok=True)
    
    app.run(debug=True, host='0.0.0.0', port=6888)
