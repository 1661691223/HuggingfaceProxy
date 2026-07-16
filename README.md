# HuggingFace Proxy

🤗 一个简洁高效的 HuggingFace 代理服务，基于 Cloudflare Workers。
体验地址：https://hf.rimuru.work

## ✨ 特性

- **零配置使用** - 直接访问即可，所有请求自动转发到 HuggingFace
- **智能重定向** - 自动处理 CDN 重定向，无需多域名配置
- **下载器脚本** - 提供 Python 下载器，支持并行下载、断点续传、HF Cache 导入
- **模块化架构** - 代码结构清晰，易于维护和扩展

## 📁 项目结构

```
hf_proxy/
├── src/                       # 源代码目录
│   ├── config.js              # 配置文件
│   ├── utils.js               # 工具函数
│   ├── handlers.js            # 请求处理器
│   ├── index.js               # 主入口
│   ├── templates/             # HTML 模板
│   │   └── home.html          # 首页模板
│   └── scripts/               # 脚本文件
│       └── hf_downloader.py   # Python 下载器
├── build.js                   # 构建脚本
├── _worker.js                 # 构建产物 (自动生成)
├── package.json
├── wrangler.toml
└── README.md
```

## 🚀 快速开始

### 部署到 Cloudflare Workers

1. Fork 本仓库
2. 在 Cloudflare Dashboard 创建 Workers 项目，连接 GitHub 仓库（Build command: `npm run build`，Deploy command: `npx wrangler deploy`）
3. 推送代码到 `main` 分支，GitHub Actions 会自动构建 `_worker.js`
4. Cloudflare Workers Builds 自动拉取最新代码并部署

部署完成后，Cloudflare 会自动分配一个 `*.workers.dev` 域名，也可以在项目设置中绑定自定义域名。

### 本地开发

```bash
# 安装依赖
npm install

# 构建并启动开发服务器
npm run dev

# 仅构建
npm run build

# 部署
npm run deploy
```

## 📖 使用方法

> ⚠️ **注意**: 不推荐使用 `huggingface-cli` 或 `snapshot_download` 搭配本代理。由于 Cloudflare 的缓存机制会覆盖或丢失 `Content-Length` / `X-Linked-Size` 等关键头信息，这会导致这些严格校验的客户端下载失败。请使用本项目自带的下载脚本，已专门优化以避开此问题。

### 直接访问

直接访问代理域名根路径即可查看使用示例和说明。

```bash
# 访问模型页面
https://your-proxy.com/bert-base-uncased

# 下载模型文件
https://your-proxy.com/bert-base-uncased/resolve/main/config.json

# API 调用
https://your-proxy.com/api/models/bert-base-uncased
```

### 使用下载器脚本

```bash
# 下载脚本
curl -O https://your-proxy.com/hf_downloader.py

# 安装依赖
pip install requests tqdm

# 基础下载
python hf_downloader.py bert-base-uncased
python hf_downloader.py openai/whisper-large-v3 --type model
python hf_downloader.py bigcode/starcoder --revision main --workers 8

# 访问控制（代理开启验证时需要）
python hf_downloader.py bert-base-uncased --proxy-token YOUR_PROXY_TOKEN

# 下载 gated 模型/数据集（需要 HuggingFace Token）
python hf_downloader.py meta-llama/Llama-2-7b --token hf_xxxxxxxx --proxy-token YOUR_PROXY_TOKEN

# 网络优化选项
python hf_downloader.py bert-base-uncased -4   # 强制使用 IPv4
python hf_downloader.py bert-base-uncased -6   # 强制使用 IPv6

# 仅列出文件（不下载）
python hf_downloader.py bert-base-uncased --list-only --proxy-token YOUR_PROXY_TOKEN

# 仅校验已有文件完整性（不下载）
python hf_downloader.py bert-base-uncased --verify-only --proxy-token YOUR_PROXY_TOKEN
```

### 完整参数列表

| 参数 | 说明 |
|------|------|
| `--type`, `-t` | 仓库类型: model / dataset / space（默认: model） |
| `--revision`, `-r` | 分支/版本（默认: main） |
| `--output`, `-o` | 输出目录 |
| `--workers`, `-w` | 并行下载数（默认: 4） |
| `--proxy`, `-p` | 代理域名 |
| `--token` | HuggingFace Token，用于访问 gated 仓库（也可设置 `HF_TOKEN` 环境变量） |
| `--proxy-token` | 代理访问 Token（也可设置 `PROXY_TOKEN` 环境变量） |
| `--list-only`, `-l` | 仅列出文件，不下载 |
| `--verify-only`, `-V` | 仅校验已有文件的完整性，不下载 |
| `--cache`, `-c` | 下载完成后导入到 HuggingFace Hub Cache |
| `--ipv4`, `-4` | 强制使用 IPv4 |
| `--ipv6`, `-6` | 强制使用 IPv6 |

### 导入到 HuggingFace Cache

使用 `--cache` 参数，下载完成后自动将文件导入到 HuggingFace Hub 标准缓存目录，`transformers` 等库可直接命中缓存，无需重新下载。

```bash
# 下载并导入到 cache
python hf_downloader.py bert-base-uncased --cache

# 指定输出目录 + cache 导入（下载完成后 output 目录会被清理）
python hf_downloader.py bert-base-uncased --output ./tmp --cache
```

导入后的缓存结构：

```
~/.cache/huggingface/hub/
  models--bert-base-uncased/
    refs/
      main                          # commit SHA
    blobs/
      {sha256}                      # 文件内容
    snapshots/
      {commit_sha}/                 # 文件名 -> blobs 的链接
        config.json
        model.safetensors
        ...
```

在 Python 中直接使用：

```python
from transformers import AutoModel, AutoTokenizer

# 直接从缓存加载，不会重新下载
model = AutoModel.from_pretrained("bert-base-uncased")
tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
```

## 🔧 工作原理

### 路由规则

| 请求路径 | 转发到 |
|---------|--------|
| `/api/models/xxx` | `huggingface.co/api/models/xxx` |
| `/bert-base/resolve/main/config.json` | `huggingface.co/bert-base/resolve/main/config.json` |
| `/redirect_to_cdn.hf.co/path/file` | `cdn.hf.co/path/file` |

### 重定向处理

当 HuggingFace 返回重定向到 CDN 节点时，Worker 会自动改写 Location：

```
原始: Location: https://cdn-lfs.hf.co/path/to/file
改写: Location: https://your-proxy.com/redirect_to_cdn-lfs.hf.co/path/to/file
```

## 📝 配置说明

### 环境变量

在 Cloudflare Workers 设置中可以配置以下环境变量：

| 变量名 | 说明 | 可选值 |
|--------|------|--------|
| `RESTRICT_BROWSER_ACCESS` | 限制浏览器直接访问代理 | `true` / `false`（默认 `false`） |
| `ACCESS_TOKEN` | 代理访问密码（设为 Secret 更安全） | 自定义字符串 |

- `RESTRICT_BROWSER_ACCESS=true` 时，浏览器只能访问首页 (`/`) 和脚本下载页面 (`/hf_downloader.py`)，其他路径将被拒绝
- `ACCESS_TOKEN` 设置后，客户端需通过 `--proxy-token` 参数、`?token=` 查询参数或 `Authorization: Bearer` 请求头提供 Token
- `ACCESS_TOKEN` 建议通过 Cloudflare Dashboard → Workers → Settings → Variables and Secrets 添加为 **Secret** 类型
- 浏览器首次访问限制页面时会弹出登录页，输入 Token 后将保存为 Cookie
- 适用于希望限制浏览器直接下载，强制使用 Python 脚本的场景

### 代码配置

编辑 `src/config.js` 可以修改：

```javascript
// 允许的上游域名列表
export const ALLOWED_UPSTREAM_DOMAINS = [
    'huggingface.co',
];

// 默认上游域名
export const DEFAULT_UPSTREAM = 'huggingface.co';

// 重定向前缀
export const REDIRECT_PREFIX = 'redirect_to_';
```

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=1661691223/HuggingfaceProxy&type=date&legend=top-left)](https://www.star-history.com/#1661691223/HuggingfaceProxy&type=date&legend=top-left)
