/**
 * HuggingFace 代理 Worker (极简版)
 *
 * 路由规则：
 * - 默认请求 → 直接转发到 huggingface.co
 * - /redirect_to_{domain}/... → 转发到 {domain}/...
 *
 * 重定向处理：
 * - 如果目标是 huggingface.co → 保持原路径
 * - 如果目标是其他允许的域名 → 添加 /redirect_to_{domain} 前缀
 *
 * 环境变量：
 * - RESTRICT_BROWSER_ACCESS: 启用访问限制 (true/false)
 *   - true: / 和 /hf_downloader.py 以外需要授权
 *   - false 或未设置: 不限制
 * - ACCESS_TOKEN: 访问 Token（可选）
 *   - 设置后所有客户端需通过 ?token=xxx 或 Authorization: Bearer xxx 提供
 *   - 仅在 RESTRICT_BROWSER_ACCESS=true 时生效
 */

import { handleHome, handleDownloaderScript, handleProxy } from './handlers.js';
import { validateBrowserAccess, isBrowserRequest, isAllowedBrowserPath } from './utils.js';

export default {
    async fetch(request, env, ctx) {
        const url = new URL(request.url);
        const hostname = url.hostname;
        const pathname = url.pathname;

        const restrictBrowserAccess = env.RESTRICT_BROWSER_ACCESS === 'true';
        const accessToken = env.ACCESS_TOKEN;

        // 处理登录表单 POST（浏览器提交 Token）
        if (restrictBrowserAccess && accessToken && request.method === 'POST' && !isAllowedBrowserPath(pathname)) {
            const formData = await request.formData();
            const submittedToken = formData.get('token');
            url.searchParams.delete('error');

            if (submittedToken === accessToken) {
                return new Response(null, {
                    status: 302,
                    headers: {
                        'Location': url.toString(),
                        'Set-Cookie': `hf_token=${accessToken}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=31536000`
                    }
                });
            }
            // Token 错误，重定向回登录页
            url.searchParams.set('error', '1');
            return Response.redirect(url.toString(), 302);
        }

        // 访问权限检查
        const { blocked, setCookie } = validateBrowserAccess(request, pathname, restrictBrowserAccess, accessToken);
        if (blocked) {
            return blocked;
        }

        // 路由分发
        let response;
        switch (true) {
            // 首页
            case pathname === '/' || pathname === '':
                response = handleHome(hostname);
                break;

            // 下载器脚本
            case pathname === '/hf_downloader.py':
                response = handleDownloaderScript(hostname);
                break;

            // 代理请求
            default:
                response = await handleProxy(request, url);
        }

        // 首次 Token 验证通过后，设置 Cookie
        if (setCookie) {
            const newHeaders = new Headers(response.headers);
            newHeaders.set('Set-Cookie', setCookie);
            response = new Response(response.body, {
                status: response.status,
                statusText: response.statusText,
                headers: newHeaders
            });
        }

        return response;
    }
};
