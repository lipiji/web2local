# web2local

给定关键词/主题，将网络上所有相关内容抓取并保存到本地，用于构建大模型训练语料。

支持格式：网页 HTML、论文 PDF、Word 文档、PPT、Excel、纯文本、图片等，中英文均可。

---

## 特性

- **全格式下载**：HTML / PDF / DOCX / PPTX / XLS / 图片 / TXT，保存到结构化目录
- **智能去重**：URL 经 SHA-256 哈希后存入 SQLite，断点续爬，不重复抓取
- **反爬对抗**：
  - `curl_cffi` TLS/JA3 指纹伪装（模拟真实 Chrome）
  - `fake-useragent` 随机 User-Agent 轮换
  - Sec-Fetch-\*、Sec-CH-UA 等完整 Client Hints 请求头
  - 每域名 Gaussian 随机延迟（默认 1.5~5 秒），模拟人类节奏
  - Referer 链路追踪（链接来源页面自动作为下一页 Referer）
  - `cloudscraper` 自动绕过 Cloudflare 验证
  - 指数退避 + 抖动重试（429/50x 自动重试最多 3 次）
- **多源搜索**：DuckDuckGo（via web4agent）+ arXiv Atom API 同时获取种子 URL
- **BFS 深度爬取**：可配置深度、最大页数、并发数

---

## 安装

```bash
git clone <repo>
cd web2local

pip install -r requirements.txt
# 安装 Chromium（无头浏览器，用于 JS 渲染页面）
patchright install chromium
```

复制环境变量模板：

```bash
cp .env.example .env
```

---

## 快速开始

```bash
# 爬取"大语言模型"相关内容，保存到 ./data/
python main.py crawl "大语言模型" --output ./data

# 爬取英文学术内容，深度 2，最多 500 页
python main.py crawl "transformer architecture" --depth 2 --max-pages 500

# 只爬同一域名，限并发 5
python main.py crawl "GPT-4" --same-domain --concurrency 5

# 查看队列状态（已抓 / 失败 / 待抓）
python main.py status

# 单独抓 arXiv PDF 链接（不爬网页）
python main.py arxiv "LLM scaling laws" --max-results 50
```

断点续爬：**直接重新运行同一命令**，已成功的 URL 会被自动跳过。

---

## 命令参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--output` / `-o` | `./data` | 输出目录 |
| `--depth` / `-d` | `3` | BFS 最大深度 |
| `--max-pages` / `-n` | `1000` | 最多保存页数后停止 |
| `--concurrency` / `-c` | `10` | 并发请求数 |
| `--search-results` | `20` | 搜索引擎种子 URL 数量 |
| `--db` | `./crawl.db` | SQLite 队列数据库路径 |
| `--follow-links` | 启用 | 从 HTML 页面发现并跟进新链接 |
| `--same-domain` | 禁用 | 只爬种子域名 |

完整环境变量见 `.env.example`。

---

## 输出目录结构

```
data/
└── <topic-slug>/
    ├── metadata.jsonl          # 每条记录：url、title、类型、路径、hash 等
    └── 20240101/
        ├── html/
        │   ├── page.html       # 原始 HTML
        │   └── page.txt        # 提取的纯文本（配套）
        ├── pdf/
        ├── docs/               # .doc / .docx
        ├── ppt/                # .ppt / .pptx
        ├── xls/                # .xls / .xlsx
        ├── images/
        └── txt/
```

`metadata.jsonl` 每行是一个 JSON，包含：

```json
{"url": "...", "title": "...", "topic": "...", "type": "html",
 "file_path": "...", "content_hash": "...", "word_count": 1234,
 "strategy": "fast", "timestamp": "2024-01-01T00:00:00+00:00"}
```

---

## 反爬策略详解

| 技术 | 工具 | 说明 |
|------|------|------|
| TLS 指纹伪装 | `curl_cffi` | 模拟 Chrome 124 的 JA3/TLS 握手 |
| JS 渲染 / 无头浏览器 | `patchright` | 绕过 Cloudflare、DataDome、PerimeterX |
| 自动降级抓取 | `web4agent` | fast → crawl4ai → browser → wayback → ddg |
| Cloudflare 绕过 | `cloudscraper` | 自动执行 JS 质询 |
| UA 轮换 | `fake-useragent` | 随机 Chrome/Edge UA，带 Client Hints |
| 域名限速 | `DomainRateLimiter` | Gaussian 分布随机延迟，默认 1.5~5s |
| Referer 链 | 引擎内置 | 子页面自动携带父页面作为 Referer |
| 重试退避 | `with_retry` | 指数退避 + 随机抖动，最多 3 次 |

---

## 开发

```bash
# 运行测试
python -m pytest tests/ -v

# 代码检查
ruff check .
ruff format .
```

项目结构：

```
web2local/
├── main.py              # CLI 入口
├── config.py            # 配置 dataclass
├── crawler/
│   ├── engine.py        # BFS 爬取引擎
│   ├── downloader.py    # 二进制文件下载（curl_cffi）
│   └── stealth.py       # 反爬层（限速/头部/重试/cloudscraper）
├── url_queue/
│   └── url_queue.py     # SQLite 异步 URL 队列
├── storage/
│   └── local_store.py   # 本地文件存储 + metadata.jsonl
├── sources/
│   └── search.py        # 搜索种子 URL（DuckDuckGo + arXiv）
└── tests/               # 62 个单元测试
```

---

## License

MIT
