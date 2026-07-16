/**
 * 请求处理器
 */

import { isAllowedUpstream, parseRequest, rewriteLocation } from './utils.js';
import HOME_HTML from './templates/home.html';
import HF_DOWNLOADER_SCRIPT from './scripts/hf_downloader.py';

/**
 * 处理首页请求
 * @param {string} hostname - 当前域名
 * @returns {Response}
 */
export function handleHome(hostname) {
    const html = HOME_HTML.replace(/\{\{HOSTNAME\}\}/g, hostname);
    return new Response(html, {
        status: 200,
        headers: { 'Content-Type': 'text/html; charset=utf-8' }
    });
}

/**
 * 处理下载器脚本请求
 * @param {string} hostname - 当前域名
 * @returns {Response}
 */
export function handleDownloaderScript(hostname) {
    const script = HF_DOWNLOADER_SCRIPT.replace(/\{\{PROXY_DOMAIN\}\}/g, hostname);
    return new Response(script, {
        status: 200,
        headers: {
            'Content-Type': 'text/x-python; charset=utf-8',
            'Content-Disposition': 'attachment; filename="hf_downloader.py"',
            'Cache-Control': 'no-cache'
        }
    });
}

/**
 * 处理代理请求
 * @param {Request} request - 原始请求
 * @param {URL} url - 解析后的 URL
 * @returns {Promise<Response>}
 */
export async function handleProxy(request, url) {
    const pathname = url.pathname;
    const proxyOrigin = url.origin;

    // 1. 解析请求，提取目标上游和实际路径
    const { upstream, path } = parseRequest(pathname);

    // 2. 验证上游域名是否被允许
    if (!isAllowedUpstream(upstream)) {
        return new Response(`Upstream not allowed: ${upstream}`, { status: 403 });
    }

    // 3. 构建发往源站的请求
    const upstreamUrl = new URL(path, `https://${upstream}`);
    upstreamUrl.search = url.search; // 保留查询参数

    const newRequest = new Request(upstreamUrl, {
        method: request.method,
        headers: request.headers,
        body: request.body,
        redirect: 'manual' // 【关键】手动拦截重定向
    });

    // 强制覆盖 Host 头
    newRequest.headers.set('Host', upstream);

    try {
        // 4. 发起请求（设置 30s 超时，避免上游无响应时 Worker 挂住）
        const signal = AbortSignal.timeout(30000);
        const response = await fetch(newRequest, { signal });

        // 5. 拦截并重写重定向
        if ([301, 302, 303, 307, 308].includes(response.status)) {
            const location = response.headers.get('Location');
            if (location) {
                const newLocation = rewriteLocation(location, proxyOrigin);
                if (newLocation) {
                    const newHeaders = new Headers(response.headers);
                    newHeaders.set('Location', newLocation);
                    return new Response(response.body, {
                        status: response.status,
                        statusText: response.statusText,
                        headers: newHeaders
                    });
                }
            }
        }

        // 6. 非重定向请求，直接返回
        return response;

    } catch (e) {
        return new Response(`Proxy Error: ${e.message}`, { status: 502 });
    }
}
