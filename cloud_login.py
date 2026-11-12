"""Manual cloud QR sign-in, followed by one approved recipient test."""
import asyncio
import base64
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.async_api import async_playwright

from app.browser import open_private_messages
from app.config import load_settings
from app.douyin import DouyinChat
from cloud_verify import guarded_task, verified_send
from scripts.login import _open_login

OUT = Path("cloud-login-output")


def encrypt_state(state):
    public_key = serialization.load_pem_public_key(Path("cloud_login_public.pem").read_bytes())
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(state).encode(), b"douyin-cloud-state-v1")
    sealed_key = public_key.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                     algorithm=hashes.SHA256(), label=None))
    return json.dumps({"version": 1, "key": base64.b64encode(sealed_key).decode(),
                       "nonce": base64.b64encode(nonce).decode(),
                       "ciphertext": base64.b64encode(ciphertext).decode()})


async def login_and_test():
    task = guarded_task(load_settings())
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        try:
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
            await _open_login(page)
            await page.wait_for_timeout(3000)
            # This is the unauthenticated login page only. Never export a chat
            # screenshot, trace, cookie, or plaintext authenticated state.
            await page.screenshot(path=str(OUT / "scan-login.png"))
            (OUT / "qr.ready").write_text("ready")
            print("Cloud browser is waiting for the account owner to scan and approve QR login", flush=True)
            deadline = asyncio.get_running_loop().time() + 600
            while asyncio.get_running_loop().time() < deadline:
                cookies = await context.cookies("https://www.douyin.com/")
                if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                    break
                await page.wait_for_timeout(2000)
            else:
                raise TimeoutError("Cloud QR login was not completed within ten minutes")
            print("Cloud login cookie detected; checking private messages", flush=True)
            await open_private_messages(page)
            state = await context.storage_state(indexed_db=True)
            (OUT / "state.encrypted.json").write_text(encrypt_state(state), encoding="utf-8")
            chat = DouyinChat(page)
            await chat.open_target(task.targets[0].name, retries=0)
            print("Approved recipient opened; sending exactly one configured message", flush=True)
            await verified_send(page, chat, task.targets[0].messages[0], task.stickers)
            print("TEST_PERSISTED: outgoing message survived reload; phone receipt still needs confirmation", flush=True)
            (OUT / "result.json").write_text(json.dumps({"status": "persisted", "sent": 1}), encoding="utf-8")
        finally:
            await browser.close()


async def main():
    OUT.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    code = 0
    try:
        await login_and_test()
    except Exception as exc:
        # Exception messages may contain private names or request details.
        # Report the category only; the known KICK guard emits a safe reason.
        print("CLOUD_TEST_FAILED: " + type(exc).__name__, flush=True)
        (OUT / "result.json").write_text(json.dumps({"status": "failed", "error_type": type(exc).__name__}), encoding="utf-8")
        code = 1
    finally:
        (OUT / "exit-code.txt").write_text(str(code))
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
