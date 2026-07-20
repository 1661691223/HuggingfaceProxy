#!/usr/bin/env python3
"""
Hugging Face 文件下载器
通过代理服务器下载 Hugging Face 仓库文件

使用方法:
    python hf_downloader.py <repo_id> [选项]
    
示例:
    python hf_downloader.py bert-base-uncased
    python hf_downloader.py openai/whisper-large-v3 --type model
    python hf_downloader.py bigcode/starcoder --revision main --workers 8
"""

import argparse
import os
import sys
import signal
import socket
import json
import hashlib
import shutil
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, quote, urlparse
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from tqdm import tqdm

__version__ = "1.8.1"

# 全局关闭标志，用于优雅退出
_shutdown_requested = False

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)

# ============== 配置 ==============
# 注意: 通过 https://xx.xxx.com/hf_downloader.py 下载时，
# Worker 会自动将下面的域名替换为请求的域名
PROXY_DOMAIN = "{{PROXY_DOMAIN}}"  # 你的代理域名
MAX_RETRIES = 3                    # 最大重试次数
CHUNK_SIZE = 64 * 1024 * 1024      # 64MB 每块
DEFAULT_WORKERS = 8                # 默认并行下载数


def check_cernet() -> bool:
    """检查是否为教育网环境"""
    try:
        #设置较短超时，避免阻塞
        resp = requests.get("http://ip-api.com/json/?fields=isp,org", timeout=3)
        if resp.ok:
            data = resp.json()
            isp = data.get("isp", "").lower()
            org = data.get("org", "").lower()
            # 常见的教育网标识
            cernet_keywords = ["cernet", "education", "university"]
            if any(k in isp for k in cernet_keywords) or any(k in org for k in cernet_keywords):
                return True
    except:
        pass
    return False


def configure_dns(force_ipv4: bool = False, force_ipv6: bool = False):
    """配置 DNS 解析优先级"""
    if not (force_ipv4 or force_ipv6):
        return
        
    original_getaddrinfo = socket.getaddrinfo
    
    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        # 如果强制指定了协议版本，则覆盖 family 参数
        if force_ipv4:
            family = socket.AF_INET
        elif force_ipv6:
            family = socket.AF_INET6
        return original_getaddrinfo(host, port, family, type, proto, flags)
        
    socket.getaddrinfo = patched_getaddrinfo


@dataclass
class FileInfo:
    """文件信息"""
    path: str           # 相对路径
    size: int           # 文件大小 (bytes)
    oid: str            # 文件 OID
    lfs: bool           # 是否是 LFS 文件
    lfs_sha256: str     # LFS 文件 SHA256 (用于完整性校验)
    download_url: str   # 下载地址


def get_hf_hub_cache() -> Path:
    """获取 HuggingFace Hub cache 根目录"""
    # 优先级: HF_HUB_CACHE > HF_HOME/hub > ~/.cache/huggingface/hub
    hub_cache = os.environ.get("HF_HUB_CACHE")
    if hub_cache:
        return Path(hub_cache)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def resolve_commit_sha(session, url: str) -> str:
    """通过 API 获取 revision 对应的 commit SHA"""
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["sha"]
    except Exception as e:
        print(f"⚠️ 获取 commit SHA 失败: {e}")
        raise


def compute_sha256(file_path: Path) -> str:
    """计算文件的 SHA256 哈希"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def compute_git_blob_sha1(file_path: Path, size: int) -> str:
    """计算文件的 Git blob SHA1 (格式: sha1("blob <size>\\0<content>"))"""
    sha1 = hashlib.sha1()
    sha1.update(f"blob {size}\0".encode())
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            sha1.update(chunk)
    return sha1.hexdigest()


def import_to_cache(output_dir: Path, repo_id: str, repo_type: str,
                    revision: str, commit_sha: str, file_list: List[FileInfo]) -> None:
    """将下载好的文件导入到 HuggingFace Hub cache 格式"""
    # 构建缓存目录名: models--org--repo / datasets--org--repo / spaces--org--repo
    prefix = {"model": "models", "dataset": "datasets", "space": "spaces"}[repo_type]
    safe_name = repo_id.replace("/", "--")
    cache_repo_dir = get_hf_hub_cache() / f"{prefix}--{safe_name}"

    blobs_dir = cache_repo_dir / "blobs"
    snapshots_dir = cache_repo_dir / "snapshots" / commit_sha
    refs_dir = cache_repo_dir / "refs"

    blobs_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n📦 正在导入到 HF cache: {cache_repo_dir}")

    for file_info in file_list:
        src_file = output_dir / file_info.path
        if not src_file.exists():
            print(f"  ⚠️ 跳过不存在的文件: {file_info.path}")
            continue

        # 计算 SHA256
        sha256_hash = compute_sha256(src_file)
        blob_path = blobs_dir / sha256_hash

        # 移动到 blobs（如已存在则跳过）
        if not blob_path.exists():
            shutil.move(str(src_file), str(blob_path))
        else:
            src_file.unlink()

        # 在 snapshots 中创建链接
        snapshot_path = snapshots_dir / file_info.path
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)

        if snapshot_path.exists() or snapshot_path.is_symlink():
            snapshot_path.unlink()

        try:
            # 相对路径符号链接
            rel_blob = os.path.relpath(str(blob_path), str(snapshot_path.parent))
            os.symlink(rel_blob, str(snapshot_path))
        except OSError:
            # Windows fallback: 复制
            shutil.copy2(str(blob_path), str(snapshot_path))

    # 写入 refs
    ref_file = refs_dir / revision
    ref_file.write_text(commit_sha)

    # 删除原始下载目录
    shutil.rmtree(output_dir, ignore_errors=True)

    print(f"✅ 导入完成: {cache_repo_dir}")
    print(f"   snapshots/{commit_sha[:12]}.../ ({len(file_list)} 个文件)")
    print(f"   refs/{revision} -> {commit_sha[:12]}...")


def setup_logging(repo_id: str) -> logging.Logger:
    """初始化日志系统，日志文件保存在脚本所在目录的 logs/ 子目录"""
    script_dir = Path(__file__).resolve().parent
    log_dir = script_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    safe_name = repo_id.replace("/", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{safe_name}_{timestamp}.log"

    logger = logging.getLogger("hf_downloader")
    logger.setLevel(logging.DEBUG)

    # 文件 handler — 记录完整详情
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    return logger


class HFDownloader:
    """Hugging Face 下载器"""

    def __init__(
        self,
        repo_id: str,
        repo_type: str = "model",
        revision: str = "main",
        output_dir: Optional[str] = None,
        proxy_domain: str = PROXY_DOMAIN,
        workers: int = DEFAULT_WORKERS,
        token: Optional[str] = None,
        proxy_token: Optional[str] = None,
        logger: Optional[logging.Logger] = None
    ):
        self.repo_id = repo_id
        self.repo_type = repo_type
        self.revision = revision
        self.proxy_domain = proxy_domain
        self.workers = workers
        self.token = token or os.environ.get("HF_TOKEN")
        self.proxy_token = proxy_token or os.environ.get("PROXY_TOKEN")
        self.logger = logger or logging.getLogger("hf_downloader")

        # 设置输出目录
        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            # 默认使用仓库名作为目录
            safe_name = repo_id.replace("/", "_")
            self.output_dir = Path.cwd() / safe_name

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 构建基础 URL (直接使用代理域名，默认转发到 huggingface.co)
        self.base_url = f"https://{proxy_domain}"

        # API 路径前缀
        if repo_type == "dataset":
            self.api_prefix = f"/api/datasets/{repo_id}"
            self.download_prefix = f"/datasets/{repo_id}/resolve/{revision}"
        elif repo_type == "space":
            self.api_prefix = f"/api/spaces/{repo_id}"
            self.download_prefix = f"/spaces/{repo_id}/resolve/{revision}"
        else:  # model
            self.api_prefix = f"/api/models/{repo_id}"
            self.download_prefix = f"/{repo_id}/resolve/{revision}"

        # Session 配置
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "HF-Downloader/1.0 (Python)"
        })
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def _url(self, path: str) -> str:
        """构建带代理 Token 的完整 URL"""
        url = f"{self.base_url}{path}"
        if self.proxy_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={self.proxy_token}"
        return url
    
    def get_file_list(self) -> List[FileInfo]:
        """获取仓库中所有文件的列表"""
        url = self._url(f"{self.api_prefix}/tree/{self.revision}")

        self.logger.info(f"获取文件列表: {url}")
        print(f"📂 正在获取文件列表: {url}")

        all_files = []
        self._fetch_tree_recursive("", all_files)

        total_size = sum(f.size for f in all_files)
        self.logger.info(f"文件列表获取完成: {len(all_files)} 个文件, 总大小 {total_size:,} bytes ({total_size / (1024**3):.2f} GB)")
        print(f"✅ 共发现 {len(all_files)} 个文件 ({self._format_size(total_size)})")
        return all_files
    
    def _fetch_tree_recursive(self, path: str, files: List[FileInfo]) -> None:
        """递归获取目录树（支持 HuggingFace API 分页）"""
        if path:
            url = self._url(f"{self.api_prefix}/tree/{self.revision}/{path}")
            params = {}
        else:
            url = self._url(f"{self.api_prefix}/tree/{self.revision}")
            params = {"recursive": "true"}

        page = 0
        total_size = 0
        while url:
            page += 1
            try:
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                # 第二页起 params 已包含在 url 的 cursor 中，避免重复
                params = {}

                items = resp.json()
                for item in items:
                    if item.get("type") == "file":
                        fpath = item["path"]
                        size = item.get("size", 0)
                        total_size += size
                        oid = item.get("oid", "")
                        lfs = item.get("lfs") is not None

                        encoded_path = quote(fpath, safe="/")
                        download_url = self._url(f"{self.download_prefix}/{encoded_path}")
                        lfs_sha256 = item.get("lfs", {}).get("oid", "") if lfs else ""

                        files.append(FileInfo(
                            path=fpath,
                            size=size,
                            oid=oid,
                            lfs=lfs,
                            lfs_sha256=lfs_sha256,
                            download_url=download_url
                        ))

                # 实时打印分页进度
                size_str = self._format_size(total_size)
                print(f"\r  Listed {len(files)} files ({size_str})...", end="", flush=True)

                # 解析 Link 头获取下一页 URL
                link_header = resp.headers.get("Link", "")
                url = None
                if link_header:
                    for part in link_header.split(","):
                        if 'rel="next"' in part:
                            raw_url = part.split(">")[0].lstrip("<")
                            # Link 头指向 huggingface.co，需要改写为走代理
                            parsed = urlparse(raw_url)
                            url = self._url(parsed.path + ("?" + parsed.query if parsed.query else ""))
                            break

            except requests.RequestException as e:
                print(f"\n⚠️ 获取文件列表失败 (第 {page} 页): {e}")
                raise

        print()  # 换行
        self.logger.info(f"文件列表共 {page} 页, {len(files)} 个文件")
    
    def download_file(self, file_info: FileInfo, progress_bar: Optional[tqdm] = None, verify: bool = True) -> bool:
        """下载单个文件，支持断点续传。verify=False 跳过末尾校验（供流水线使用）"""
        global _shutdown_requested
        output_path = self.output_dir / file_info.path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 文件已存在且完整 → 跳过（始终校验，已存在文件校验很快）
        if output_path.exists() and output_path.stat().st_size == file_info.size:
            if self._verify_integrity(output_path, file_info):
                self.logger.info(f"跳过(已存在): {file_info.path}")
                if progress_bar:
                    progress_bar.update(file_info.size)
                return True
            # 校验失败，删除重新下载
            self.logger.warning(f"已存在文件校验失败，重新下载: {file_info.path}")
            output_path.unlink()

        # 断点续传：记录已有字节数
        resume_pos = 0
        if output_path.exists():
            resume_pos = output_path.stat().st_size
            if resume_pos >= file_info.size:
                # 本地文件异常（比预期大），删除重下
                output_path.unlink()
                resume_pos = 0
        if resume_pos > 0:
            self.logger.info(f"断点续传: {file_info.path} @ {resume_pos:,}/{file_info.size:,} bytes")

        for attempt in range(MAX_RETRIES):
            try:
                headers = {}
                if resume_pos > 0:
                    headers["Range"] = f"bytes={resume_pos}-"

                resp = self.session.get(
                    file_info.download_url,
                    headers=headers,
                    stream=True,
                    timeout=60,
                    allow_redirects=True
                )

                if resp.status_code == 416:
                    # Range 越界，文件可能已被远程修改，删除重下
                    output_path.unlink()
                    resume_pos = 0
                    continue

                resp.raise_for_status()

                # 服务器正确响应 Range → 追加模式
                # attempt==0 才补充已下载字节，重试时进度条已有上次累积，避免重复计数
                if resp.status_code == 206:
                    if progress_bar and resume_pos > 0 and attempt == 0:
                        progress_bar.update(resume_pos)
                    mode = "ab"
                else:
                    # 服务器不支持 Range → 重新下载
                    if resume_pos > 0:
                        output_path.unlink()
                        resume_pos = 0
                    mode = "wb"

                with open(output_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if _shutdown_requested:
                            return False
                        if chunk:
                            f.write(chunk)
                            if progress_bar:
                                progress_bar.update(len(chunk))

                # 完整性校验（仅 verify=True 时）
                if verify:
                    file_size_mb = file_info.size / (1024 * 1024)
                    tqdm.write(f"  🔍 校验中: {file_info.path} ({file_size_mb:.1f} MB)...")
                    if not self._verify_integrity(output_path, file_info):
                        output_path.unlink()
                        resume_pos = 0
                        tqdm.write(f"  ❌ 校验失败: {file_info.path}")
                        raise ValueError(f"校验失败: {file_info.path}")
                    tqdm.write(f"  ✅ 校验通过: {file_info.path}")

                return True

            except Exception as e:
                error_str = str(e)
                if 'IncompleteRead' in error_str or 'Connection broken' in error_str:
                    self.logger.warning(f"连接中断: {file_info.path} - {e}")
                    tqdm.write(f"⚠️ 连接中断 ({attempt + 1}/{MAX_RETRIES}): {file_info.path}，将断点续传")
                else:
                    self.logger.error(f"下载失败: {file_info.path} - {e}")
                    tqdm.write(f"⚠️ 下载失败 ({attempt + 1}/{MAX_RETRIES}): {file_info.path} - {e}")
                if attempt < MAX_RETRIES - 1:
                    import time
                    time.sleep(2 ** attempt)

        return False

    def _verify_integrity(self, file_path: Path, file_info: FileInfo) -> bool:
        """校验文件完整性：LFS 用 SHA256，普通文件用 Git blob SHA1"""
        if file_info.lfs and file_info.lfs_sha256:
            actual = compute_sha256(file_path)
            expected = file_info.lfs_sha256
        elif file_info.oid:
            actual = compute_git_blob_sha1(file_path, file_info.size)
            expected = file_info.oid
        else:
            return True  # 没有校验信息，跳过
        if actual != expected:
            self.logger.warning(f"校验失败: {file_info.path} 期望={expected[:16]}... 实际={actual[:16]}...")
            tqdm.write(f"⚠️ 校验失败: {file_info.path}")
            tqdm.write(f"   期望: {expected[:16]}...")
            tqdm.write(f"   实际: {actual[:16]}...")
            return False
        return True

    def _verify_single_file(self, file_info: FileInfo, verify_bar: Optional[tqdm] = None) -> bool:
        """校验单个已下载文件（供校验线程池调用）"""
        output_path = self.output_dir / file_info.path
        if not output_path.exists() or output_path.stat().st_size != file_info.size:
            if verify_bar:
                verify_bar.update(1)
            return False
        if not self._verify_integrity(output_path, file_info):
            self.logger.warning(f"校验失败: {file_info.path}")
            tqdm.write(f"  ❌ 校验失败: {file_info.path}")
            if verify_bar:
                verify_bar.update(1)
            return False
        self.logger.info(f"校验通过: {file_info.path}")
        if verify_bar:
            verify_bar.update(1)
        return True

    def verify_only(self, files: Optional[List[FileInfo]] = None):
        """仅校验文件完整性，不下载"""
        if files is None:
            files = self.get_file_list()

        if not files:
            print("⚠️ 没有找到任何文件")
            return

        print(f"\n🔍 校验模式: 共 {len(files)} 个文件")
        print(f"📁 本地目录: {self.output_dir}\n")

        ok_count = 0
        missing_count = 0
        mismatch_count = 0
        no_info_count = 0

        for f in files:
            file_path = self.output_dir / f.path

            # 确定期望值
            if f.lfs and f.lfs_sha256:
                expected = f.lfs_sha256
                algo = "SHA256"
            elif f.oid:
                expected = f.oid
                algo = "GitSHA1"
            else:
                expected = None
                algo = None

            # 文件不存在
            if not file_path.exists():
                missing_count += 1
                expected_short = expected[:16] + "..." if expected else "(无校验信息)"
                print(f"  ✗ 缺失  {f.path}")
                print(f"         期望 {algo}: {expected_short}")
                continue

            # 无校验信息
            if expected is None:
                no_info_count += 1
                print(f"  ~ 跳过  {f.path} (无校验信息)")
                continue

            # 计算实际值
            if f.lfs and f.lfs_sha256:
                actual = compute_sha256(file_path)
            else:
                actual = compute_git_blob_sha1(file_path, f.size)

            if actual == expected:
                ok_count += 1
                print(f"  ✓ 通过  {f.path}")
            else:
                mismatch_count += 1
                print(f"  ✗ 失败  {f.path}")
                print(f"         期望 {algo}: {expected[:16]}...")
                print(f"         实际 {algo}: {actual[:16]}...")

        total = len(files)
        print(f"\n{'='*60}")
        print(f"总计 {total} 个文件:  ✓通过 {ok_count}   ✗缺失 {missing_count}   ✗不匹配 {mismatch_count}", end="")
        if no_info_count:
            print(f"   ~跳过 {no_info_count}", end="")
        print()

    def download_all(self, files: Optional[List[FileInfo]] = None) -> Dict[str, Any]:
        """下载所有文件"""
        if files is None:
            files = self.get_file_list()
        
        if not files:
            print("⚠️ 没有找到任何文件")
            return {"success": 0, "failed": 0, "skipped": 0}
        
        # 计算总大小
        total_size = sum(f.size for f in files)
        self.logger.info(f"开始下载: {len(files)} 个文件, 总大小 {total_size:,} bytes ({total_size / (1024**3):.2f} GB), 并行数 {self.workers}")
        print(f"\n📦 准备下载 {len(files)} 个文件, 总大小: {self._format_size(total_size)}")
        print(f"📁 输出目录: {self.output_dir}")
        print(f"🔧 并行数: {self.workers}\n")
        
        # 显示文件列表
        print("=" * 60)
        print(f"{'文件名':<45} {'大小':>12}")
        print("=" * 60)
        for f in files[:10]:  # 只显示前10个
            name = f.path if len(f.path) <= 45 else "..." + f.path[-42:]
            print(f"{name:<45} {self._format_size(f.size):>12}")
        if len(files) > 10:
            print(f"... 还有 {len(files) - 10} 个文件")
        print("=" * 60 + "\n")
        
        # 创建进度条
        progress = tqdm(
            total=total_size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc="下载进度"
        )
        
        results = {"success": 0, "failed": 0, "failed_files": []}
        lock = threading.Lock()

        # 两个线程池：下载（自定义 workers） + 校验（固定 4 线程）
        download_executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="dl")
        verify_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="verify")
        print(f"🔧 下载线程: {self.workers}, 校验线程: 4\n")

        try:
            # Phase 1: 提交所有下载（跳过末尾校验）
            d_futures = {download_executor.submit(self.download_file, f, progress, verify=False): f
                         for f in files}

            # Phase 2: 下载完成立即排队校验
            verify_queue = []
            for df in as_completed(d_futures):
                if _shutdown_requested:
                    break
                f = d_futures[df]
                try:
                    if df.result():
                        verify_queue.append(f)
                    else:
                        with lock:
                            results["failed"] += 1
                            results["failed_files"].append(f.path)
                except Exception:
                    with lock:
                        results["failed"] += 1
                        results["failed_files"].append(f.path)

            # Phase 3: 校验已下载文件（4 线程并行，与剩余下载重叠）
            if verify_queue and not _shutdown_requested:
                verify_bar = tqdm(
                    total=len(verify_queue),
                    unit="file",
                    desc="校验进度"
                )
                v_futures = {verify_executor.submit(self._verify_single_file, f, verify_bar): f
                             for f in verify_queue}
                for vf in as_completed(v_futures):
                    if _shutdown_requested:
                        break
                    f = v_futures[vf]
                    try:
                        if vf.result():
                            with lock:
                                results["success"] += 1
                        else:
                            with lock:
                                results["failed"] += 1
                                results["failed_files"].append(f.path)
                            output_path = self.output_dir / f.path
                            if output_path.exists():
                                output_path.unlink()
                    except Exception:
                        with lock:
                            results["failed"] += 1
                            results["failed_files"].append(f.path)
                verify_bar.close()

        except KeyboardInterrupt:
            self.logger.warning("用户中断下载 (Ctrl+C)")
            print("\n\n⏸️  正在停止... (已下载的文件下次可续传)")
        finally:
            download_executor.shutdown(wait=False)
            verify_executor.shutdown(wait=False)
            progress.close()

        # 打印结果
        self.logger.info(f"下载结束: 成功={results['success']}, 失败={results['failed']}, 总计={len(files)}")
        print("\n" + "=" * 60)
        print(f"✅ 下载完成: {results['success']}/{len(files)} 个文件成功")
        if results["failed"] > 0:
            print(f"❌ 失败文件: {results['failed']} 个")
            for f in results["failed_files"]:
                print(f"   - {f}")
        print("=" * 60)

        return results
    
    @staticmethod
    def _format_size(size: int) -> str:
        """格式化文件大小"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} PB"


def _on_interrupt(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        # 第二次 Ctrl+C → 强制退出
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os._exit(1)
    _shutdown_requested = True
    print("\n⏸️  正在停止... (再次 Ctrl+C 强制退出)")


def check_update(proxy_domain: str):
    """检查脚本版本，如有新版本则自动更新"""
    try:
        resp = requests.get(f"https://{proxy_domain}/version", timeout=10)
        remote_version = resp.text.strip()

        from packaging.version import Version
    except ImportError:
        # 简单字符串比较
        if remote_version != __version__:
            _do_update(proxy_domain, remote_version)
        return
    except Exception:
        return  # 检查失败，静默跳过

    try:
        if Version(remote_version) > Version(__version__):
            _do_update(proxy_domain, remote_version)
    except Exception:
        return


def _do_update(proxy_domain: str, new_version: str):
    """下载新版本脚本并替换当前文件"""
    print(f"\n🔄 发现新版本 v{new_version} (当前 v{__version__})，正在自动更新...")
    try:
        resp = requests.get(f"https://{proxy_domain}/hf_downloader.py", timeout=60)
        resp.raise_for_status()

        current_file = Path(__file__).resolve()
        # 备份旧文件
        backup = current_file.with_suffix(".py.bak")
        shutil.copy2(current_file, backup)

        # 写入新版本
        with open(current_file, "w", encoding="utf-8") as f:
            f.write(resp.text)

        print(f"✅ 更新完成 v{new_version}，请重新运行命令")
        # 还原备份（新版写入成功后清理）
        if backup.exists():
            backup.unlink()
        sys.exit(0)
    except Exception as e:
        print(f"⚠️ 自动更新失败: {e}")
        # 恢复备份
        backup = Path(__file__).resolve().with_suffix(".py.bak")
        if backup.exists():
            shutil.copy2(backup, Path(__file__).resolve())
            backup.unlink()


def main():
    signal.signal(signal.SIGINT, _on_interrupt)

    # 提前解析 repo_id 和 proxy 用于日志初始化和版本检查
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("repo_id", nargs="?", default="unknown")
    pre_parser.add_argument("--proxy", "-p", default=PROXY_DOMAIN)
    pre_args, _ = pre_parser.parse_known_args()

    # 检查更新
    check_update(pre_args.proxy)
    logger = setup_logging(pre_args.repo_id)
    logger.info("=" * 60)
    logger.info(f"HF Downloader 启动")

    parser = argparse.ArgumentParser(
        description="通过代理下载 Hugging Face 仓库文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    %(prog)s bert-base-uncased
    %(prog)s openai/whisper-large-v3 --type model
    %(prog)s bigcode/starcoder --revision main --workers 8
    %(prog)s microsoft/phi-2 --output ./my_models
        """
    )
    
    parser.add_argument("repo_id", help="仓库 ID (例如: bert-base-uncased 或 openai/whisper-large-v3)")
    parser.add_argument("--type", "-t", choices=["model", "dataset", "space"], 
                        default="model", help="仓库类型 (默认: model)")
    parser.add_argument("--revision", "-r", default="main", 
                        help="分支/版本 (默认: main)")
    parser.add_argument("--output", "-o", help="输出目录")
    parser.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS,
                        help=f"并行下载数 (默认: {DEFAULT_WORKERS})")
    parser.add_argument("--proxy", "-p", default=PROXY_DOMAIN,
                        help=f"代理域名 (默认: {PROXY_DOMAIN})")
    parser.add_argument("--token", help="HuggingFace Token，用于访问 gated 模型 (也可设置 HF_TOKEN 环境变量)")
    parser.add_argument("--proxy-token", help="代理访问 Token (也可设置 PROXY_TOKEN 环境变量)")
    parser.add_argument("--list-only", "-l", action="store_true",
                        help="仅列出文件，不下载")
    parser.add_argument("--verify-only", "-V", action="store_true",
                        help="仅校验已有文件的完整性，不下载")
    parser.add_argument("--ipv4", "-4", action="store_true", help="强制使用 IPv4")
    parser.add_argument("--ipv6", "-6", action="store_true", help="强制使用 IPv6")
    parser.add_argument("--cache", "-c", action="store_true",
                        help="下载完成后导入到 HuggingFace Hub cache (支持 from_pretrained 直接加载)")
    
    args = parser.parse_args()

    # 处理 IP 协议选择
    if args.ipv4 and args.ipv6:
        print("❌ 错误: 不能同时指定 -4 和 -6")
        sys.exit(1)
        
    use_ipv6 = args.ipv6
    use_ipv4 = args.ipv4
    
    # 如果未指定，自动检测是否为教育网
    if not (use_ipv6 or use_ipv4):
        if check_cernet():
            print("🎓 检测到教育网环境，自动启用 IPv6 优化")
            use_ipv6 = True
            
    if use_ipv6:
        print("🌐 已启用强制 IPv6 解析")
        configure_dns(force_ipv6=True)
    elif use_ipv4:
        print("🌐 已启用强制 IPv4 解析")
        configure_dns(force_ipv4=True)
    
    logger.info(f"配置: repo={args.repo_id}, type={args.type}, revision={args.revision}, proxy={args.proxy}, workers={args.workers}")

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║          🤗 Hugging Face 代理下载器                          ║
╠══════════════════════════════════════════════════════════════╣
║  仓库: {args.repo_id:<53} ║
║  类型: {args.type:<53} ║
║  分支: {args.revision:<53} ║
║  代理: {args.proxy:<53} ║
╚══════════════════════════════════════════════════════════════╝
""")

    downloader = HFDownloader(
        repo_id=args.repo_id,
        repo_type=args.type,
        revision=args.revision,
        output_dir=args.output,
        proxy_domain=args.proxy,
        workers=args.workers,
        token=args.token,
        proxy_token=args.proxy_token,
        logger=logger
    )
    
    if args.list_only:
        files = downloader.get_file_list()
        print("\n📋 文件列表:")
        print("=" * 70)
        for f in files:
            lfs_tag = "[LFS]" if f.lfs else ""
            print(f"{f.path:<50} {downloader._format_size(f.size):>12} {lfs_tag}")
        print("=" * 70)
        print(f"总计: {len(files)} 个文件, {downloader._format_size(sum(f.size for f in files))}")
    elif args.verify_only:
        downloader.verify_only()
    else:
        files = downloader.get_file_list()
        results = downloader.download_all(files)

        # 下载成功后导入到 HF cache
        if args.cache and results["failed"] == 0:
            try:
                commit_sha = resolve_commit_sha(
                    downloader.session,
                    downloader._url(f"{downloader.api_prefix}/revision/{downloader.revision}")
                )
                import_to_cache(
                    downloader.output_dir, args.repo_id, args.type,
                    args.revision, commit_sha, files
                )
            except Exception as e:
                print(f"\n❌ 导入 cache 失败: {e}")
                print(f"   文件仍保留在: {downloader.output_dir}")

    logger.info("HF Downloader 结束")
    for handler in logger.handlers:
        handler.close()


if __name__ == "__main__":
    main()
