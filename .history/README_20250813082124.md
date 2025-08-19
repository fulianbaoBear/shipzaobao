# 航运早报 - 智能语音航运资讯平台

基于 Flask 与 Minimax 语音合成的航运行业资讯聚合与播报工具。聚合多家权威来源，自动去重、排序、翻译为中文，支持一键生成语音与分享。

## ✨ 关键特性

- 🚢 多源航运新闻聚合：`Splash 247`、`Ship & Bunker`、`信德海事网`、`gCaptain`
- 🧠 智能清洗与去重：过滤导航/栏目/社交链接，仅保留真实新闻标题
- 🇨🇳 自动中文化：英文标题自动翻译为中文（失败回退原文）
- 🔗 可点击原文：早报正文中的每条新闻都带原文链接
- 📈 智能排序：
  - 优先级1：天津/渤海湾/环渤海等关键词
  - 优先级2：中国港口与国内相关词
  - 优先级3：前十班轮公司（MSC、马士基、达飞、中远海运/OOCL、赫伯罗特、ONE、长荣、HMM、阳明、以星等）
- 🌤️ 天气滚动条：默认显示“天津”天气，自动追加“渤海湾”海面风力；天气现象与风向均为中文
- 💾 缓存与历史：按天缓存与留存，支持历史查看与复制
- 🎵 语音合成：接入 Minimax，高质量语音生成与在线播放/下载/分享
- 📱 现代海蓝主题 UI：深浅蓝配色、船舶图标、移动端适配

## 🗂 新闻来源与规则

- 基于来源域名的链接级过滤与白名单规则，剔除 `/category/`、`/region/`、`/prices` 等栏目路径
- 提供 RSS/静态解析兜底，国内网络不佳时仍尽力返回有效结果
- 可在 `app.py` 的 `SHIPPING_SOURCES` 中增删来源

## ⚙️ 可配置项（界面设置）

- Minimax：`Group ID`、`API Key`、模型与音色等参数
- 天气位置：`weather_location`（默认“天津”）。支持中文/英文地名，示例：`天津`、`Shanghai`、`Singapore`、`New York`

## 🚀 本地快速开始（Windows）

```powershell
# 1. 创建并激活虚拟环境
python -m venv .venv
.\.venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 安装 Playwright 浏览器
python -m playwright install chromium

# 4. 启动
python app.py
# 访问：http://localhost:6888/aizaobao/
```

> 首次进入建议在“设置”中填写 Minimax 信息与“天气位置”。点击“刷新”可强制抓取当日最新航运早报。

## 🔌 API（均带前缀 `/aizaobao`）

- 配置
  - `GET  /aizaobao/api/config` 获取当前会话配置
  - `POST /aizaobao/api/config` 更新配置
- 新闻
  - `GET  /aizaobao/api/news` 获取新闻（命中当日缓存）
  - `POST /aizaobao/api/refresh-news` 强制刷新新闻（忽略缓存）
- 历史
  - `GET  /aizaobao/api/history` 历史列表
  - `GET  /aizaobao/api/history/<cache_date>` 指定日期详情
- 天气
  - `GET  /aizaobao/api/weather` 天气与海面风力（中文现象与风向）
- 语音
  - `POST /aizaobao/api/generate-audio` 生成音频
  - `POST /aizaobao/api/download-audio` 下载音频

## 🛠️ 自定义与扩展

- 新闻来源：编辑 `app.py` 中的 `SHIPPING_SOURCES`
- 优先级关键词：
  - `PRIORITY_KEYWORDS_LEVEL1`（天津/渤海湾/环渤海等）
  - `PRIORITY_KEYWORDS_LEVEL2`（中国港口相关词）
  - `LINER_KEYWORDS`（顶级班轮公司中英文别名）
- 海区映射：在 `MARINE_ALIAS` 中新增城市→海区（如：`"青岛" → 黄海`、`"舟山" → 东海`）

## 🧩 运行说明（更多）

- 缓存：当日首次抓取写入 `cache/news_YYYY-MM-DD.json`，当天后续命中缓存；保留最近30天
- 生成音频：保存到 `static/audio`，页面提供在线播放/下载/分享
- 静态资源缓存：模板对 `style.css` 追加版本参数，避免浏览器缓存旧样式

## ❓ 常见问题

- 天气仍显示英文现象？
  - 已默认请求中文，同时内置英文→中文兜底映射；如仍英文，多为第三方返回异常，稍后再试
- 强制刷新失败或无新闻？
  - 点击“刷新”或检查网络；如长期无结果，可在 `SHIPPING_SOURCES` 增加更多来源
- Playwright 报错（浏览器未安装/权限问题）？
  - 重新执行 `python -m playwright install chromium`

## 📄 许可证

本项目采用 MIT License。

---

享受更专业的“航运早报”与有声资讯体验！
