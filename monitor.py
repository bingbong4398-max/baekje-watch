#!/usr/bin/env python3
"""Monitor Baekje Cultural Land's Nov. 7 Nakhwa ticket entry and notify Telegram.

This checks whether the exact event ticket product heading is rendered in the
provided Naver Map ticket pane. It does not confirm inventory or buy tickets.
"""
import asyncio
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

TARGET_URL = (
    'https://map.naver.com/p/entry/place/12019039'
    '?c=15.00,0,0,0,dh'
    '&placePath=%2Fticket%3Ffrom%3Dmap%26fromPanelNum%3D1'
    '%26additionalHeight%3D76%26timestamp%3D202610221639'
    '%26locale%3Dko%26svcName%3Dmap_pcv5%26entry%3Dbmp'
)
STATE_FILE = Path('.monitor_state.json')
TARGET = re.compile(r'2026\s*년\s*백제문화단지\s*낙화놀이\s*[（(]\s*11\s*/\s*7\s*[)）]')
SEOUL = ZoneInfo('Asia/Seoul')


def send_telegram(message):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not (token and chat_id):
        raise RuntimeError('TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets must be set')
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    payload = urllib.parse.urlencode({
        'chat_id': chat_id,
        'text': message,
        'disable_web_page_preview': 'true',
    }).encode('utf-8')
    with urllib.request.urlopen(urllib.request.Request(url, data=payload, method='POST'), timeout=20) as r:
        data = json.load(r)
    if not data.get('ok'):
        raise RuntimeError('Telegram did not confirm the message')


def load_state():
    try:
        return json.loads(STATE_FILE.read_text('utf-8')).get('visible', False)
    except (OSError, ValueError):
        return False


def save_state(visible):
    STATE_FILE.write_text(json.dumps({'visible': visible}, ensure_ascii=False) + '\n', encoding='utf-8')


async def rendered_texts(page):
    items = []
    # Naver place/ticket UI often uses cross-origin frames.
    for index, frame in enumerate(page.frames):
        try:
            text = await frame.locator('body').inner_text(timeout=4000)
            if text:
                items.append((index, frame.url, text))
        except Exception as exc:
            print(f'frame[{index}] text read error: {type(exc).__name__}', flush=True)
    return items


async def inspect_page():
    Path('debug').mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page(locale='ko-KR', timezone_id='Asia/Seoul', viewport={'width': 1400, 'height': 950})
        try:
            try:
                await page.goto(TARGET_URL, wait_until='domcontentloaded', timeout=45000)
            except PlaywrightTimeoutError:
                print('Navigation timed out; inspecting whatever has loaded.', flush=True)
            latest = []
            for attempt in range(4):
                await page.wait_for_timeout(3500)
                latest = await rendered_texts(page)
                if any(TARGET.search(text) for _, _, text in latest):
                    break

            all_text = '\n'.join(t for _, _, t in latest)
            print('Final browser URL:', page.url, flush=True)
            print('Frames:', len(latest), flush=True)
            print('Text preview:', re.sub(r'\s+', ' ', all_text)[:450], flush=True)
            if os.environ.get('DEBUG_SCREENSHOT', '').lower() == 'true':
                try:
                    await page.screenshot(path='debug/last_page.png', full_page=False, timeout=15000)
                except Exception as exc:
                    print(f'Could not capture screenshot: {type(exc).__name__}', flush=True)
                Path('debug/frames.txt').write_text(
                    '\n\n'.join(f'--- frame {idx}: {url} ---\n{text[:2500]}' for idx, url, text in latest),
                    encoding='utf-8',
                )
            if any(TARGET.search(t) for _, _, t in latest):
                return 'found'
            # Only reset a previously found state if a recognizable booking page loaded.
            # Loading errors, anti-bot blocks, or unexpected page structures must not
            # be treated as a genuine disappearance/reappearance.
            valid_booking_ui = (
                '백제문화단지' in all_text
                and any(word in all_text for word in ('예매하기', '예약하기', '예매', '예약'))
                and len(all_text) > 100
            )
            return 'not_found' if valid_booking_ui else 'unknown'
        except Exception as exc:
            print('Inspection exception:', repr(exc), flush=True)
            return 'unknown'
        finally:
            await browser.close()


async def main():
    now = datetime.now(SEOUL)
    if now.year > 2026 or (now.year == 2026 and (now.month, now.day) > (11, 7)):
        print('Event already ended. Disable the scheduled workflow.', flush=True)
        return

    status = await inspect_page()
    old = load_state()
    print(f'Status={status}; prior_visible={old}; KST={now.isoformat()}', flush=True)

    if os.environ.get('SEND_TEST', '').lower() == 'true':
        names = {'found': '행사 상품명 발견', 'not_found': '상품명 미표시', 'unknown': '페이지 확인 불가(접속 차단/구조 변경 가능)'}
        send_telegram('🧪 [낙화놀이 감시 테스트]\n텔레그램 봇 연결 성공!\n웹페이지 확인: ' + names[status] + '\n' + TARGET_URL)

    if status == 'found' and not old:
        send_telegram(
            '🚨 [백제문화단지 낙화놀이 예매 화면 발견]\n'
            '2026년 백제문화단지 낙화놀이(11/7) 상품명이 페이지에 나타났습니다.\n'
            '※ 실제 잔여 수량/결제 가능 여부는 직접 확인해야 합니다.\n'
            + TARGET_URL
        )
        save_state(True)
        print('New appearance: notification sent and state saved.', flush=True)
    elif status == 'not_found' and old:
        save_state(False)
        print('Ticket heading disappeared; ready to alert on next appearance.', flush=True)
    elif status == 'unknown':
        print('Could not reliably inspect the page; previous state retained.', flush=True)
    else:
        print('No state change; no event alert needed.', flush=True)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print('FATAL:', repr(exc), file=sys.stderr, flush=True)
        sys.exit(1)
