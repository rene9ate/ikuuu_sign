"""
Playwright async 方案：共享浏览器 + asyncio.gather，同一线程无竞态
"""
import os
import json
import time
import sys
import asyncio
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

try:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
except ImportError as e:
    print(f"❌ Playwright 导入失败: {e}")
    print("请运行: pip install playwright==1.48.0")
    sys.exit(1)

from urllib.parse import urlparse
import requests

# ─────────────── 配置 ───────────────
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ikuuu_cookies.json")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

DOMAINS = ["ikuuu.top", "ikuuu.pw"]
RESULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkin_result.json")

def get_cookie_key(email, base_url):
    host = urlparse(base_url).netloc.lower()
    return f"{email}@@{host}"

def _load_cookie_store_unlocked():
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        tprint(f"  ⚠️ 加载cookie失败: {e}")
        return {}

def _save_cookie_store_unlocked(store):
    try:
        temp_file = COOKIE_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, COOKIE_FILE)
    except Exception as e:
        tprint(f"  ⚠️ 保存cookie失败: {e}")

def save_session_cookie(email, base_url, cookie_str):
    with _cookie_lock:
        store = _load_cookie_store_unlocked()
        key = get_cookie_key(email, base_url)
        store[key] = cookie_str
        _save_cookie_store_unlocked(store)

def load_session_cookie(email, base_url):
    with _cookie_lock:
        store = _load_cookie_store_unlocked()
        key = get_cookie_key(email, base_url)
        return store.get(key)

def clear_session_cookie(email, base_url):
    with _cookie_lock:
        store = _load_cookie_store_unlocked()
        key = get_cookie_key(email, base_url)
        if key in store:
            del store[key]
            _save_cookie_store_unlocked(store)

# ─────────────── 邮箱脱敏 ───────────────
def mask_email(email):
    idx = email.find('@')
    if idx <= 0:
        return email
    return email[0] + '***' + email[idx:]


# ─────────────── 账号获取 ───────────────
def get_accounts():
    accounts = []
    account_str = os.getenv('ACCOUNTS')
    if account_str and account_str.strip():
        for line in account_str.strip().splitlines():
            line = line.strip()
            if line and ':' in line:
                email, pwd = line.split(':', 1)
                accounts.append((email.strip(), pwd.strip()))
    else:
        print("❌ 未配置 ACCOUNTS 环境变量")
        return None
    print(f"📋 找到 {len(accounts)} 个账户")
    return accounts

# ─────────────── 验证 Cookie ───────────────
def validate_cookie(session, base_url):
    """返回: True=有效, False=过期, None=网络异常（保留cookie）"""
    try:
        resp = session.get(
            base_url + "/user",
            headers={"User-Agent": USER_AGENT},
            timeout=15,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return False
        if "/auth/login" in resp.url.lower():
            return False
        if "var originBody" in resp.text or "剩余流量" in resp.text:
            return True
        return False
    except requests.exceptions.RequestException as e:
        tprint(f"  ⚠️ 请求异常: {e}")
        return None

# ─────────────── 签到 ───────────────
def do_checkin(session, base_url):
    try:
        resp = session.post(
            base_url + '/user/checkin',
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": USER_AGENT,
            },
            timeout=20,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return False, f"签到失败(状态码{resp.status_code})"
        data = resp.json()
        if data.get('ret') == 1:
            return True, f"成功 | {data.get('msg', '')}"
        msg = str(data.get('msg', '未知'))
        if any(p in msg for p in ['已签到', '已经签到', '已簽到', 'already']):
            return True, f"成功 | {msg}"
        return False, f"签到失败: {msg}"
    except Exception as e:
        return False, f"签到异常: {e}"

# ─────────────── 线程安全打印 ───────────────
_print_lock = Lock()
_cookie_lock = Lock()

def tprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

# ─────────────── Playwright 登录（context 级别，async）───────────────
async def login_in_context_async(context, email, password, base_url, timeout_ms=60000):
    """
    在已有 context 中完成登录流程，返回 cookies 或 None
    """
    page = await context.new_page()

    async def abort_unused(route):
        if route.request.resource_type in ("image", "font", "media"):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", abort_unused)

    login_url = f"{base_url}/auth/login"

    try:
        print(f"  🌐 打开登录页: {login_url}")
        await page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
        print(f"  ✅ 页面加载完成: {await page.title()}")

        await page.wait_for_selector('#email', timeout=10000)
        print(f"  📝 填写账号密码...")

        await page.fill('#email', email)
        await page.fill('#password', password)

        await page.wait_for_selector('.embed-captcha', timeout=10000)

        try:
            await page.click('.geetest_btn_click', timeout=5000)
            print(f"  ✅ 已点击验证按钮")
        except Exception:
            print(f"  ℹ️ 未找到验证按钮，可能无需点击")

        await page.wait_for_function(
            "() => window.Captcha && window.Captcha.isReady()",
            timeout=20000
        )

        login_btn = await page.query_selector('button[type="submit"]')
        if not login_btn:
            return None, "未找到登录按钮"

        print(f"  🖱️ 模拟真人移动并点击登录...")
        await page.click('button[type="submit"]')

        try:
            await page.wait_for_url(
                lambda url: "/user" in url or "/dashboard" in url or "checkin" in url,
                timeout=15000,
            )
            print(f"  ✅ 检测到跳转: {page.url}")
        except PwTimeout:
            current_url = page.url
            content = await page.content()
            if "/auth/login" not in current_url or "签到" in content or "剩余流量" in content:
                print(f"  ⚠️ 未检测到跳转，但页面内容可能已成功: {current_url}")
            else:
                return None, "登录后未检测到期望的页面跳转"

        pw_cookies = await context.cookies()
        if not pw_cookies:
            return None, "未获取到 Cookie"

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in pw_cookies if c.get("name"))
        print(f"  🍪 获取到 {len(pw_cookies)} 个 Cookie")
        return cookie_str, None

    except PwTimeout as e:
        return None, f"操作超时: {e}"
    except Exception as e:
        return None, f"异常: {e}"
    finally:
        await page.close()


# ─────────────── 共享浏览器登录（async）───────────────
async def login_account_async(browser, email, password, domains, timeout_ms=60000):
    """共享 browser，async 版本"""
    for domain in domains:
        base_url = f"https://{domain}"
        masked = mask_email(email)
        context = None
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            """)
            tprint(f"  [{masked}] 尝试 {domain}")
            pw_cookies, err = await login_in_context_async(context, email, password, base_url, timeout_ms)
            if pw_cookies:
                return email, pw_cookies, None, domain
            tprint(f"  [{masked}] {domain} 失败: {err}")
        except Exception as e:
            tprint(f"  [{masked}] {domain} 异常: {e}")
        finally:
            if context:
                await context.close()
    return email, None, "所有域名登录失败", None


# ─────────────── Cookie 预检（sync，在线程池中执行）───────────────
def cookie_checkin(email, password):
    """返回 (email, result_dict|None, need_login:bool, should_retry:bool)"""
    for domain in DOMAINS:
        base_url = f"https://{domain}"
        cached = load_session_cookie(email, base_url)
        if not cached:
            continue
        sess = requests.session()
        sess.headers["Cookie"] = cached
        status = validate_cookie(sess, base_url)
        if status is None:
            tprint(f"  ⚠️ {mask_email(email)} [{domain}] 网络异常，保留 cookie 下次再试")
            return email, None, True, True
        if status is True:
            ok_s, msg = do_checkin(sess, base_url)
            masked = mask_email(email)
            r = {"email": masked, "success": ok_s, "message": msg, "domain": domain}
            tprint(f"  🍪 [{domain}] {masked} {'✅' if ok_s else '❌'} {msg}")
            return email, r, False, False
        tprint(f"  🗑️ [{domain}] {mask_email(email)} cookie 已过期，清理")
        clear_session_cookie(email, base_url)
    return email, None, True, False


def cookie_checkin_with_retry(idx, email, password, max_retries=2):
    """返回 (idx, email, result_dict|None, need_login:bool)"""
    for attempt in range(max_retries):
        ret_email, result, need_login, should_retry = cookie_checkin(email, password)
        if result or not need_login:
            return idx, ret_email, result, need_login
        if not should_retry:
            break
        if attempt < max_retries - 1:
            tprint(f"  🔄 {mask_email(email)} 第{attempt+1}次失败，{attempt+1}s后重试")
            time.sleep(attempt + 1)
    return idx, email, None, True


# ─────────────── 异步主流程 ───────────────
async def async_main():
    print("🚀 iKuuu Playwright 签到脚本启动 (async)")
    print("=" * 50)

    headless = os.getenv("PLAYWRIGHT_HEADLESS", "1") != "0"
    print(f"{'🕶️ 无头模式' if headless else '🖥️  有头模式'}")

    accounts = get_accounts()
    if not accounts:
        sys.exit(1)

    results = []
    loop = asyncio.get_running_loop()

    # ── 第一轮：并行试 cookie（在线程池中执行 sync 代码）──
    checkin_tasks = []
    task_to_idx = {}
    executor = ThreadPoolExecutor(max_workers=20)
    for idx, (email, pwd) in enumerate(accounts, 1):
        coro = loop.run_in_executor(executor, cookie_checkin_with_retry, idx, email, pwd)
        task = asyncio.ensure_future(coro)
        checkin_tasks.append(task)
        task_to_idx[task] = idx

    need_login = []
    done_set, pending_set = await asyncio.wait(checkin_tasks, timeout=120)

    for task in pending_set:
        task.cancel()
        idx = task_to_idx[task]
        email, pwd = accounts[idx - 1]
        tprint(f"  ⚠️ {mask_email(email)} cookie 签到超时，转入浏览器登录")
        need_login.append((idx, email, pwd))

    for task in done_set:
        idx = task_to_idx[task]
        try:
            _, ret_email, result, should_login = task.result()
            if result:
                results.append(result)
            if should_login:
                email, pwd = accounts[idx - 1]
                need_login.append((idx, email, pwd))
        except Exception as e:
            tprint(f"  ⚠️ 账号 {idx} cookie 签到异常: {e}")
            email, pwd = accounts[idx - 1]
            need_login.append((idx, email, pwd))

    executor.shutdown(wait=False)

    # 第二轮：共享浏览器 + asyncio.gather
    if need_login:
        print(f"\n🎭 共享浏览器，async 并行登录 {len(need_login)} 个账号...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            login_tasks = [
                login_account_async(browser, email, pwd, DOMAINS)
                for idx, email, pwd in need_login
            ]
            login_results = await asyncio.gather(*login_tasks)
            await browser.close()

        # 处理所有登录结果
        for (idx, email, pwd), (ret_email, cookie_str, err, domain) in zip(need_login, login_results):
            masked = mask_email(email)
            base_url = f"https://{domain}" if domain else None

            if err or not cookie_str:
                print(f"  ❌ {masked} 登录失败: {err}")
                results.append({"email": masked, "success": False, "message": f"登录失败: {err}", "domain": domain or "all"})
                continue

            if base_url:
                sess = requests.session()
                sess.headers["Cookie"] = cookie_str
                ok_s, msg = do_checkin(sess, base_url)

                if ok_s:
                    save_session_cookie(email, base_url, cookie_str)
                    with open("cookie_updated.flag", "w") as f:
                        f.write("1")
                    print(f"  💾 {masked} Cookie 已保存 ({domain})")

                results.append({"email": masked, "success": ok_s, "message": msg, "domain": domain})
                icon = "✅" if ok_s else "❌"
                print(f"  {icon} {masked} {msg}")

    # 输出结果文件供 workflow 读取
    has_failure = any(not r["success"] for r in results)
    summary_lines = []
    print("\n📊 汇总:")
    print("=" * 50)
    for r in results:
        icon = "✅" if r["success"] else "❌"
        line = f"{icon} {r['email']} | {r['message']}"
        print(line)
        summary_lines.append(line)
    print("=" * 50)
    print("🏁 执行完成")

    try:
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump({"results": results, "summary": "\n".join(summary_lines), "has_failure": has_failure}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(async_main())
