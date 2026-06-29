/**
 * 工具函数
 */

import { ALLOWED_UPSTREAM_DOMAINS, DEFAULT_UPSTREAM, REDIRECT_PREFIX } from './config.js';
import LOGIN_HTML from './templates/login.html';

/**
 * 判断是否是允许的上游域名
 * @param {string} hostname - 要检查的域名
 * @returns {boolean}
 */
export function isAllowedUpstream(hostname) {
    // 直接匹配已知域名
    if (ALLOWED_UPSTREAM_DOMAINS.includes(hostname)) {
        return true;
    }
    // 允许所有 .hf.co 结尾的 CDN 节点
    if (hostname.endsWith('.hf.co')) {
        return true;
    }
    return false;
}

/**
 * 解析请求路径，提取目标上游和实际路径
 * @param {string} pathname - 请求路径
 * @returns {{ upstream: string, path: string }}
 */
export function parseRequest(pathname) {
    // 检查是否有 redirect_to_ 前缀
    // 格式: /redirect_to_{domain}/path/to/resource
    const prefixPattern = new RegExp(`^/${REDIRECT_PREFIX}([^/]+)(/.*)$`);
    const match = pathname.match(prefixPattern);
    
    if (match) {
        // 有前缀，提取域名和路径
        return {
            upstream: match[1],
            path: match[2]
        };
    }
    
    // 无前缀，使用默认上游
    return {
        upstream: DEFAULT_UPSTREAM,
        path: pathname
    };
}

/**
 * 重写重定向 Location
 * @param {string} location - 原始 Location
 * @param {string} proxyOrigin - 代理服务器的 origin
 * @returns {string | null} - 重写后的 Location，如果不需要重写则返回 null
 */
export function rewriteLocation(location, proxyOrigin) {
    try {
        const locUrl = new URL(location);
        const locHost = locUrl.hostname;

        // 检查是否是允许的上游域名
        if (!isAllowedUpstream(locHost)) {
            return null;
        }

        // 构造新的重定向 URL
        if (locHost === DEFAULT_UPSTREAM) {
            // 默认上游，直接使用原路径
            return `${proxyOrigin}${locUrl.pathname}${locUrl.search}`;
        } else {
            // 其他上游，添加 redirect_to_ 前缀
            return `${proxyOrigin}/${REDIRECT_PREFIX}${locHost}${locUrl.pathname}${locUrl.search}`;
        }
    } catch (e) {
        console.error("Location parse error:", e);
        return null;
    }
}

/**
 * 判断请求是否来自浏览器
 * @param {Request} request - 请求对象
 * @returns {boolean}
 */
export function isBrowserRequest(request) {
    const accept = request.headers.get('Accept') || '';
    const userAgent = request.headers.get('User-Agent') || '';

    // 检查 Accept 头是否包含 HTML
    const acceptsHtml = accept.includes('text/html');

    // 检查 User-Agent 是否包含浏览器特征
    // 排除 curl、wget、python-requests、go-http 等工具
    const browserPatterns = [
        'Mozilla/', 'Chrome/', 'Safari/', 'Firefox/', 'Edge/', 'Opera/',
        'MSIE', 'Trident/', 'SamsungBrowser/', 'UCBrowser/'
    ];
    const isBrowserUA = browserPatterns.some(pattern => userAgent.includes(pattern));

    // 排除明确的非浏览器工具
    const nonBrowserPatterns = [
        'curl/', 'wget/', 'Python-requests', 'python-requests', 'requests/',
        'go-http-tool', 'Java/', 'okhttp', 'axios/', 'node-fetch', 'deno/',
        'libwww-perl', 'lwp-trivial', 'Git/', 'git/', 'GitHub-Hookshot',
        'HTTPie/', 'http.rb/', 'Ruby/', 'PHP/', 'PostmanRuntime/',
        'insomnia/', 'Paw/', 'REST Client', 'Swift/', 'Darwin/',
        'CF-Workers', 'Cloudflare-Workers', 'Worker/', 'dart:io'
    ];
    const isToolUA = nonBrowserPatterns.some(pattern => userAgent.includes(pattern));

    return acceptsHtml && isBrowserUA && !isToolUA;
}

/**
 * 检查路径是否为允许浏览器访问的页面
 * @param {string} pathname - 请求路径
 * @returns {boolean}
 */
export function isAllowedBrowserPath(pathname) {
    const allowedPaths = ['/', '', '/hf_downloader.py'];
    return allowedPaths.includes(pathname);
}

/**
 * 验证访问权限
 *
 * 规则：
 *  - / 和 /hf_downloader.py 始终允许
 *  - RESTRICT_BROWSER_ACCESS 不为 "true" 时，不做任何限制
 *  - RESTRICT_BROWSER_ACCESS 为 "true" 时：
 *    - 如果设置了 ACCESS_TOKEN，所有客户端需提供 Token
 *      - 浏览器无 Token 时弹出登录页面
 *      - 非浏览器客户端无 Token 时返回 403
 *    - 如果未设置 ACCESS_TOKEN，仅限制浏览器访问
 *
 * Token 传递方式（优先级从高到低）：
 *  1. Cookie: hf_token=xxx（浏览器登录后自动设置）
 *  2. Query: ?token=xxx
 *  3. Header: Authorization: Bearer xxx
 *
 * @param {Request} request - 请求对象
 * @param {string} pathname - 请求路径
 * @param {boolean} restrictBrowserAccess - 是否启用访问限制
 * @param {string} accessToken - 访问 Token，为空则不校验
 * @returns {{ blocked: Response | null, setCookie: string | null }}
 */
export function validateBrowserAccess(request, pathname, restrictBrowserAccess, accessToken) {
    if (!restrictBrowserAccess) {
        return { blocked: null, setCookie: null };
    }

    // 首页和下载器脚本始终允许
    if (isAllowedBrowserPath(pathname)) {
        return { blocked: null, setCookie: null };
    }

    // 如果设置了 ACCESS_TOKEN，走 Token 校验（所有客户端均需提供）
    if (accessToken) {
        // 从 Cookie 中读取 token
        const cookieHeader = request.headers.get('Cookie') || '';
        const cookieToken = cookieHeader.split(';')
            .map(c => c.trim())
            .find(c => c.startsWith('hf_token='))
            ?.slice(9);

        if (cookieToken === accessToken) {
            return { blocked: null, setCookie: null };
        }

        // 从 Query 参数和 Header 中读取
        const url = new URL(request.url);
        const queryToken = url.searchParams.get('token');
        const authHeader = request.headers.get('Authorization');
        const headerToken = authHeader?.startsWith('Bearer ') ? authHeader.slice(7) : null;

        // Query/Header 验证通过 → 设置 Cookie
        if (queryToken === accessToken || headerToken === accessToken) {
            return {
                blocked: null,
                setCookie: `hf_token=${accessToken}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=31536000`
            };
        }

        // Token 不匹配
        const browser = isBrowserRequest(request);

        // 浏览器 POST（登录表单提交）
        if (browser && request.method === 'POST') {
            // 重定向回当前页面，带上 error 参数
            url.searchParams.delete('token');
            url.searchParams.set('error', '1');
            return {
                blocked: Response.redirect(url.toString(), 302),
                setCookie: null
            };
        }

        // 浏览器 GET → 显示登录页面
        if (browser) {
            return {
                blocked: new Response(LOGIN_HTML, {
                    status: 401,
                    headers: { 'Content-Type': 'text/html; charset=utf-8' }
                }),
                setCookie: null
            };
        }

        // 非浏览器 → 403
        return {
            blocked: new Response('Access denied: invalid or missing token', {
                status: 403,
                headers: { 'Content-Type': 'text/plain; charset=utf-8' }
            }),
            setCookie: null
        };
    }

    // 未设置 Token，走浏览器 UA 检查
    if (isBrowserRequest(request)) {
        return {
            blocked: new Response(
                '浏览器访问受限。请使用 API 客户端（curl、wget、Python 等）访问模型文件。\n\n' +
                '允许访问的页面：\n' +
                '  - / (首页)\n' +
                '  - /hf_downloader.py (下载脚本)',
                {
                    status: 403,
                    headers: { 'Content-Type': 'text/plain; charset=utf-8' }
                }
            ),
            setCookie: null
        };
    }

    return { blocked: null, setCookie: null };
}
