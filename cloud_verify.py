"""Conservative cloud test: a local bubble alone is not delivery proof."""
from collections import Counter
import asyncio
import json
import logging
from urllib.parse import urlsplit

import app.main as runner
from app.browser import RiskControlError, open_private_messages, verify_login
from app.douyin import PageOperationError
from app.sender import OUTGOING_MESSAGES

LOGGER = logging.getLogger("douyin_sender")
original_send = runner.send_message
original_load_task = runner.load_task


def guarded_task(settings):
    task = original_load_task(settings)
    if len(task.targets) != 1 or task.targets[0].name != "Jessicattt":
        raise PageOperationError("Cloud test is restricted to the approved test recipient")
    if len(task.targets[0].messages) != 1:
        raise PageOperationError("Cloud test permits exactly one message")
    return task


async def fingerprints(page):
    # Ignore timestamps, React HTML and signed query strings. Keep content only
    # in memory; never write private conversations or cookies into public logs.
    values = await page.locator(OUTGOING_MESSAGES).evaluate_all("""elements =>
        elements.filter(e => e.getClientRects().length).map(e => {
            const c = e.querySelector('[data-e2e="msg-item-content"]');
            if (!c) return null;
            const text = (c.innerText || '').replace(/\\s+/g, ' ').trim();
            const images = [...c.querySelectorAll('img')].map(i => {
                try { return new URL(i.src).pathname; } catch { return ''; }
            });
            return text || images.some(Boolean) ? JSON.stringify([text, images]) : null;
        }).filter(Boolean)
    """)
    return Counter(values)


def persisted_delta(before, sent, reloaded):
    delta = sent - before
    return bool(delta) and any(reloaded[key] > before[key] for key in delta)


async def stop_on_kick(page, operation):
    """Abort the current send before retrying a server-rejected session."""
    rejected = asyncio.Event()
    inspectors = set()

    async def inspect(response):
        try:
            body = await asyncio.wait_for(response.body(), timeout=5)
            if len(body) <= 4096:
                data = json.loads(body)
                if isinstance(data, dict) and data.get("decision") == "KICK":
                    rejected.set()
        except (Exception, asyncio.CancelledError):
            # Ordinary successful replies are protobuf, not JSON. The existing
            # UI and reload checks remain required for those replies.
            pass

    def response_received(response):
        url = urlsplit(response.url)
        if url.hostname == "imapi.douyin.com" and url.path == "/v1/message/send":
            task = asyncio.create_task(inspect(response))
            inspectors.add(task)
            task.add_done_callback(inspectors.discard)

    page.on("response", response_received)
    send_task = asyncio.create_task(operation())
    rejection_task = asyncio.create_task(rejected.wait())
    try:
        await asyncio.wait((send_task, rejection_task), return_when=asyncio.FIRST_COMPLETED)
        if send_task.done() and inspectors:
            await asyncio.gather(*tuple(inspectors), return_exceptions=True)
        if rejected.is_set():
            LOGGER.error("抖音发送接口返回 KICK：立即停止当前任务，不再重试，需要在运行环境中重新登录验证")
            raise RiskControlError("发送接口拒绝当前会话（KICK），已停止发送和重试")
        return await send_task
    finally:
        page.remove_listener("response", response_received)
        cleanup = (send_task, rejection_task, *tuple(inspectors))
        for task in cleanup:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup, return_exceptions=True)


async def verified_send(page, chat, message, stickers):
    return await stop_on_kick(page, lambda: _verified_send(page, chat, message, stickers))


async def _verified_send(page, chat, message, stickers):
    before = await fingerprints(page)
    await original_send(page, chat, message, stickers)
    # A delayed failure must not get counted as success after a two-second grace.
    await page.wait_for_timeout(12_000)
    await verify_login(page, timeout_ms=3_000)
    sent = await fingerprints(page)
    LOGGER.info("Cloud verification: outgoing before=%d, after=%d",
                sum(before.values()), sum(sent.values()))
    if not (sent - before):
        raise PageOperationError("No unambiguous new outgoing content; delivery uncertain, no automatic resend")
    # Rebuild the conversation from the service, discarding optimistic UI state.
    await page.reload(wait_until="domcontentloaded")
    await open_private_messages(page)
    await chat.open_target("Jessicattt", retries=0)
    await page.wait_for_timeout(3_000)
    await verify_login(page, timeout_ms=3_000)
    reloaded = await fingerprints(page)
    LOGGER.info("Cloud verification: outgoing after reload=%d", sum(reloaded.values()))
    if not persisted_delta(before, sent, reloaded):
        raise PageOperationError("New message did not persist after reload; delivery uncertain, no automatic resend")
    LOGGER.info("Cloud verification: new outgoing content survived reload; recipient receipt still requires confirmation")


runner.load_task = guarded_task
runner.send_message = verified_send
