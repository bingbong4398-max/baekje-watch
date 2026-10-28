#!/usr/bin/env python3
"""Monitor Baekje Cultural Land's Nov. 7 Nakhwa ticket entry and notify Telegram.

Checks (1) reappearance of the exact event entry on Naver Map (once per run), and
(2) the 2026-11-07 calendar date on the direct Naver booking page (~60s).
It never logs in, places a reservation, or confirms payment.
"""
import asyncio
import json
import os
import re
import sys
import time
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
DIRECT_URL = 'https://m.booking.naver.com/booking/5/bizes/557207/items/8117739'
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
        state = json.loads(STATE_FILE.read_text('utf-8'))
        return {
            'visible': bool(state.get('visible', False)),
            'calendar': state.get('calendar', 'unknown'),
        }
    except (OSError, ValueError, TypeError):
        return {'visible': False, 'calendar': 'unknown'}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False) + '\n', encoding='utf-8')


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


async def scan_and_scroll(page):
    """Check event text, including ticket items loaded only after scrolling."""
    latest = []

    async def check():
        nonlocal latest
        latest = await rendered_texts(page)
        return any(TARGET.search(text) for _, _, text in latest)

    for _ in range(3):
        await page.wait_for_timeout(2200)
        if await check():
            return True, latest

    total_steps = 0
    for frame in list(page.frames):
        try:
            count = await frame.evaluate("""() => {
                const scrollable = [...document.querySelectorAll('body *')].filter(el => {
                    const box = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return /auto|scroll/.test(style.overflowY)
                        && el.scrollHeight - el.clientHeight > 120
                        && box.width >= 230 && box.width <= 850
                        && box.height >= 170 && box.height <= 1300;
                });
                const root = document.scrollingElement;
                if (root && root.scrollHeight - root.clientHeight > 120 && !scrollable.includes(root)) {
                    scrollable.push(root);
                }
                scrollable.sort((a, b) =>
                    (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
                );
                window.__nakhwaScrollTargets = scrollable.slice(0, 3);
                return window.__nakhwaScrollTargets.length;
            }""")
        except Exception as exc:
            print('Scrollable-container inspection error:', type(exc).__name__, flush=True)
            continue

        print('Scrollable containers in frame:', count, flush=True)
        for index in range(count):
            stable_bottom = 0
            for _ in range(20):
                try:
                    info = await frame.evaluate("""idx => {
                        const el = window.__nakhwaScrollTargets?.[idx];
                        if (!el || !el.isConnected) return null;
                        const before = el.scrollTop;
                        el.scrollTop = Math.min(
                            before + Math.max(180, el.clientHeight * 0.8),
                            el.scrollHeight - el.clientHeight
                        );
                        return {
                            before,
                            after: el.scrollTop,
                            end: el.scrollHeight - el.clientHeight
                        };
                    }""", index)
                except Exception as exc:
                    print('Scroll step error:', type(exc).__name__, flush=True)
                    break
                if not info:
                    break
                total_steps += 1
                await page.wait_for_timeout(550)
                if await check():
                    print('Found event after scrolling; steps:', total_steps, flush=True)
                    return True, latest
                # Allow several attempts at bottom for lazy-loaded entries.
                if info['after'] >= info['end'] - 4:
                    stable_bottom += 1
                    if stable_bottom >= 3:
                        break
                else:
                    stable_bottom = 0

    await page.wait_for_timeout(1200)
    found = await check()
    print('Finished ticket-list scan; scroll steps:', total_steps, flush=True)
    return found, latest


async def inspect_page():
    Path('debug').mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        page = await browser.new_page(locale='ko-KR', timezone_id='Asia/Seoul', viewport={'width': 1400, 'height': 950})
        try:
            try:
                await page.goto(TARGET_URL, wait_until='domcontentloaded', timeout=45000)
            except PlaywrightTimeoutError:
                print('Navigation timed out; inspecting whatever has loaded.', flush=True)
            found, latest = await scan_and_scroll(page)
            all_text = '\n'.join(t for _, _, t in latest)
            print('Final browser URL:', page.url, flush=True)
            print('Frames:', len(latest), flush=True)
            print('Text preview:', re.sub(r'\s+', ' ', all_text)[:450], flush=True)
            if os.environ.get('DEBUG_SCREENSHOT', '').lower() == 'true':
                try:
                    await page.screenshot(path='debug/last_page.png', full_page=False, timeout=15000)
                except Exception as exc:
                    print('Could not capture screenshot:', type(exc).__name__, flush=True)
                Path('debug/frames.txt').write_text(
                    '\n\n'.join(f'--- frame {idx}: {url} ---\n{text[:2500]}' for idx, url, text in latest),
                    encoding='utf-8',
                )
            if found:
                return 'found'
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


# Read the exact booking date's rendered calendar cell; do not infer availability
# merely because the word "매진" is absent elsewhere on the page.
CALENDAR_JS = r"""() => {
  const tidy = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el) return false;
    const css = getComputedStyle(el), b = el.getBoundingClientRect();
    return css.display !== 'none' && css.visibility !== 'hidden' && b.width > 0 && b.height > 0;
  };
  const leaf = el => [...el.children].every(c => !visible(c));
  const text = el => tidy(el.innerText || el.textContent);
  const nodes = [...document.querySelectorAll('body *')];
  // Identify the month heading itself, not '2026.11.07' in an event description.
  const headings = nodes.filter(el => visible(el) && leaf(el)
       && /^2026\s*[.\/년-]\s*0?11\s*(월)?\.?$/.test(text(el)));
  if (!headings.length) return {status:'unknown', reason:'11월 달력 제목 미발견'};
  for (const h of headings) {
    const hb = h.getBoundingClientRect();
    const sevens = nodes.filter(el => visible(el) && leaf(el)
      && /^0?7\s*(일)?$/.test(text(el)) && (() => {
        const r=el.getBoundingClientRect();
        return r.top >= hb.bottom - 8 && r.top <= hb.bottom + 410
          && Math.abs(r.left - hb.left) < 550;
      })());
    for (const seven of sevens) {
      let tile = seven;
      let hasSold = false, blocked = false, actionable = false;
      for (let depth=0; depth < 5 && tile; depth++, tile=tile.parentElement) {
        const r=tile.getBoundingClientRect(), t=text(tile);
        // Do not examine entire week, month or the whole page.
        if (r.width > 180 || r.height > 145 || t.length > 65) break;
        const meta = [tile.className, tile.getAttribute('aria-label'),
                      tile.getAttribute('title'), tile.getAttribute('data-state')].join(' ');
        if (/매진|마감|품절|예약불가|예매불가|sold.?out/i.test(t + ' ' + meta)) hasSold = true;
        if (tile.matches('[disabled], [aria-disabled="true"], [inert]')
          || /disabled|inactive|unavailable/i.test(meta)
          || getComputedStyle(tile).pointerEvents === 'none') blocked = true;
        if (tile.matches('button,a,[role="button"]') || tile.tabIndex >= 0
          || getComputedStyle(tile).cursor === 'pointer') actionable = true;
      }
      if (hasSold) return {status:'sold_out', reason:'11/7 칸에 매진/마감 확인'};
      if (actionable && !blocked) return {status:'open', reason:'11/7 매진 없음 + 날짜 선택 가능'};
    }
  }
  return {status:'unknown', reason:'11/7 칸의 선택 가능 상태를 검증하지 못함'};
}"""

NEXT_MONTH_JS = r"""() => {
  const norm=s=>(s||'').replace(/\s+/g,' ').trim();
  const seen=e=>{if(!e)return false;const b=e.getBoundingClientRect(),c=getComputedStyle(e);
    return b.width>0&&b.height>0&&c.display!=='none'&&c.visibility!=='hidden'};
  const heads=[...document.querySelectorAll('body *')].filter(el => seen(el)
    && /^2026\s*[.\/년-]\s*0?10\s*(월)?\.?$/.test(norm(el.textContent))
    && [...el.children].every(c => !seen(c)));
  for (const h of heads) {
    const hb=h.getBoundingClientRect();
    for(let p=h.parentElement, depth=0; p&&depth<4; p=p.parentElement,depth++) {
      const buttons=[...p.querySelectorAll('button, [role="button"], a')].filter(seen)
        .map(el=>({el,rect:el.getBoundingClientRect()}))
        .filter(o => o.rect.left >= hb.right-3 && Math.abs(o.rect.top-hb.top)<50)
        .sort((a,b)=>a.rect.left-b.rect.left);
      if (buttons.length) {buttons[0].el.click(); return true;}
    }
  }
  return false;
}"""


async def inspect_calendar():
    """Check 11/7 only. Unknown means no alert, never 'available'."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = await browser.new_context(locale='ko-KR', timezone_id='Asia/Seoul',
                                            viewport={'width': 1100, 'height': 950})
        page = await context.new_page()
        try:
            try:
                await page.goto(DIRECT_URL, wait_until='domcontentloaded', timeout=18000)
            except PlaywrightTimeoutError:
                print('Direct booking loading timeout; inspecting partial page.', flush=True)
            await page.wait_for_timeout(1000)

            # The booking calendar is sometimes below the "예매하기" button.
            for frame in list(page.frames):
                for role in ('button', 'link'):
                    try:
                        locator = frame.get_by_role(role, name=re.compile(r'^\s*(?:예매하기|예약하기)\s*$'))
                        if await locator.count() and await locator.first.is_visible():
                            await locator.first.click(timeout=2200)
                            print('Opened booking calendar.', flush=True)
                            break
                    except Exception:
                        pass
            await page.wait_for_timeout(750)

            for attempt in range(3):
                pages = list(context.pages)
                for active_page in pages:
                    for frame in list(active_page.frames):
                        try:
                            result = await frame.evaluate(CALENDAR_JS)
                            if result['status'] in ('open', 'sold_out'):
                                print('Calendar:', result, flush=True)
                                await diagnostic_calendar(active_page)
                                return result['status']
                        except Exception as exc:
                            print('Calendar inspect error:', type(exc).__name__, flush=True)
                moved = False
                if attempt < 2:
                    for active_page in pages:
                        for frame in list(active_page.frames):
                            try:
                                # Only move forward when the *October* calendar header is visible.
                                # If already in November but the day cell is unrecognized, remain UNKNOWN.
                                is_october = await frame.evaluate(r"""() => [...document.querySelectorAll('body *')].some(el => {
                                    const t = (el.innerText || '').trim();
                                    const box = el.getBoundingClientRect();
                                    return box.width > 0 && box.height > 0
                                        && /^2026\s*[.\/년-]\s*0?10\s*(월)?\.?$/.test(t)
                                        && [...el.children].every(c => !c.getBoundingClientRect().width);
                                })""")
                                if not is_october:
                                    continue
                                # First try the named next-month control.
                                next_btn = frame.get_by_role('button', name=re.compile(r'다음\s*(?:달|월)|next\s*month', re.I))
                                if await next_btn.count() and await next_btn.first.is_visible():
                                    await next_btn.first.click(timeout=2000)
                                    moved = True
                                    break
                                # Fall back to the unlabeled chevron beside '2026.10'.
                                if await frame.evaluate(NEXT_MONTH_JS):
                                    moved = True
                                    break
                            except Exception as exc:
                                print('Next-month attempt:', type(exc).__name__, flush=True)
                        if moved:
                            break
                if not moved:
                    break
                await page.wait_for_timeout(800)

            await diagnostic_calendar(context.pages[-1])
            print('Calendar status UNKNOWN (no verified 11/7 date cell).', flush=True)
            return 'unknown'
        except Exception as exc:
            print('Calendar exception:', repr(exc), flush=True)
            return 'unknown'
        finally:
            await browser.close()


async def diagnostic_calendar(page):
    if os.environ.get('DEBUG_SCREENSHOT', '').lower() == 'true':
        Path('debug').mkdir(exist_ok=True)
        try:
            await page.screenshot(path='debug/calendar_page.png', timeout=6000)
        except Exception as exc:
            print('Calendar screenshot error:', type(exc).__name__, flush=True)


async def check_limited(coro, seconds, label):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except Exception as exc:
        print(f'{label} timed out or failed: {type(exc).__name__}: {exc}', flush=True)
        return 'unknown'


async def main():
    now = datetime.now(SEOUL)
    if now.date().isoformat() > '2026-11-07':
        print('Event date passed; skip monitoring.', flush=True)
        return
    state = load_state()
    started = time.monotonic()
    manual = os.environ.get('SEND_TEST', '').lower() == 'true'
    # One GitHub scheduled run every 5 minutes. Inspect at 0, 60, 120, 180, 240s.
    # The first check also scans the general Naver ticket list; later checks
    # focus on the specific booking calendar to save time and reduce traffic.
    rounds = 1 if manual else 5
    for i in range(rounds):
        print(f'-- Poll {i + 1}/{rounds}, Korea time: {datetime.now(SEOUL).isoformat()} --', flush=True)
        if i == 0:
            product, calendar = await asyncio.gather(
                check_limited(inspect_page(), 50, 'Ticket list'),
                check_limited(inspect_calendar(), 40, 'Calendar'))
        else:
            product = 'unknown'
            calendar = await check_limited(inspect_calendar(), 40, 'Calendar')
        print(f'Product={product}; Calendar={calendar}; previous={state}', flush=True)

        if manual:
            prod_labels = {'found':'상품 발견','not_found':'상품 미표시','unknown':'화면 확인 불가'}
            cal_labels = {'open':'11/7 날짜 선택 가능','sold_out':'11/7 매진','unknown':'11/7 달력 판독 불가'}
            send_telegram('🧪 [낙화놀이 1분 감시 테스트]\n'
                          + '상품: ' + prod_labels[product] + '\n'
                          + '예약 달력: ' + cal_labels[calendar] + '\n' + DIRECT_URL)

        modified = False
        if product == 'found' and not state['visible']:
            send_telegram('🔔 [낙화놀이 상품 발견]\n11월 7일 예매 상품이 목록에 나타났습니다.\n' + DIRECT_URL)
            state['visible'] = True
            modified = True
        elif product == 'not_found' and state['visible']:
            state['visible'] = False
            modified = True

        if calendar == 'open' and state['calendar'] != 'open':
            send_telegram('🚨 [11/7 낙화놀이 예약 가능 가능성]\n'
                          '11월 7일 달력의 매진 문구가 보이지 않고 날짜가 선택 가능한 것으로 감지됐어요.\n'
                          '실제 예약 가능한지 즉시 확인하세요!\n' + DIRECT_URL)
            state['calendar'] = 'open'
            modified = True
        elif calendar == 'sold_out' and state['calendar'] != 'sold_out':
            state['calendar'] = 'sold_out'
            modified = True

        if modified:
            save_state(state)
            print('Updated state saved.', flush=True)
        else:
            print('No new confirmed state; no alert.', flush=True)

        if i < rounds - 1:
            # Start checks roughly every 60s (even when a check takes time).
            pause = max(0, started + 60 * (i + 1) - time.monotonic())
            print(f'Next check in {pause:.0f}s.', flush=True)
            await asyncio.sleep(pause)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print('FATAL:', repr(exc), file=sys.stderr, flush=True)
        sys.exit(1)
