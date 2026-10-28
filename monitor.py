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
  const norm = x => (x || '').replace(/\s+/g,' ').trim();
  const body = document.body?.innerText || '';
  // We ONLY inspect November. The Naver UI initially opens in October.
  if (!/(?:^|\n)\s*2026\s*[.]\s*11\s*(?:\n|$)/.test(body)) {
    return {status:'unknown',reason:'November month heading not visible'};
  }
  const dates = [...document.querySelectorAll('button.calendar_date')];
  const valid = dates.filter(btn => {
    const t=norm(btn.innerText || btn.textContent);
    // Naver date button may include "7" or "7 매진".
    return /^0?7(?:$|\s|일|매진|마감|예약|예매|품절)/.test(t);
  });
  if (!valid.length) return {status:'unknown',reason:'No Nov 7 calendar_date button', dateButtons:dates.length};

  const results = [];
  for (const button of valid) {
    const ancestors = [button];
    for(let p=button.parentElement, n=0; p && n<4; p=p.parentElement,n++){
      // Stop before whole weeks / table: don't mix other dates with 7.
      if (p.querySelectorAll('button.calendar_date').length !== 1) break;
      ancestors.push(p);
    }
    const localText=norm(ancestors.map(x=>x.innerText).join(' '));
    const meta=ancestors.map(x=>[x.className,x.getAttribute('aria-label'),x.getAttribute('title'),x.getAttribute('data-state')].join(' ')).join(' ');
    const hasSoldOut=/매진|판매\s*마감|예약\s*마감|예매\s*마감|품절|sold.?out/i.test(localText+' '+meta);
    const disabled=button.disabled || button.getAttribute('aria-disabled')==='true' ||
      /(?:^|\s)(?:unselectable|disabled|dayoff|inactive|soldout|sold-out)(?:\s|$)/i.test(String(button.className)) ||
      getComputedStyle(button).pointerEvents==='none';
    results.push({text:localText.slice(0,100),buttonClass:String(button.className),disabled,
                  soldOut:hasSoldOut,buttonHTML:button.outerHTML.slice(0,550)});
  }
  if(results.some(x=>x.soldOut))
    return {status:'sold_out', reason:'11/7 tile explicitly says sold out',details:results};
  if(results.length===1 && !results[0].disabled)
    return {status:'open', reason:'11/7 is a selectable date, no sold-out marking',details:results};
  // Do not notify merely because "매진" is missing while date is disabled.
  return {status:'unavailable',reason:'Nov 7 is not selectable',details:results};
}"""


async def visible_month(frame):
    """Read the displayed calendar month, not the text in the event title."""
    try:
        text = await frame.locator('body').inner_text(timeout=3500)
    except Exception:
        return None
    match = re.search(r'(?m)^\s*2026\s*\.\s*(10|11)\s*$', text)
    return int(match.group(1)) if match else None


async def navigate_to_november(context):
    """Use the exact button observed in user's diagnostic: BUTTON.btn_next, aria-label=다음달."""
    pages = list(context.pages)
    for page in pages:
        for frame in list(page.frames):
            month = await visible_month(frame)
            if month == 11:
                print('Calendar navigation: already November.',flush=True)
                return frame
            if month != 10:
                continue
            print('Calendar navigation: October visible; locating button.btn_next.',flush=True)
            candidates = [
                frame.locator('button.btn_next:not(.disabled)'),
                frame.locator('button[aria-label="다음달"]'),
                frame.get_by_role('button', name='다음달'),
            ]
            for loc in candidates:
                try:
                    if not await loc.count() or not await loc.first.is_visible():
                        continue
                    await loc.first.click(timeout=5500)
                    for _ in range(12):
                        await page.wait_for_timeout(500)
                        new_month=await visible_month(frame)
                        if new_month == 11:
                            print('Calendar navigation successful: 2026.10 -> 2026.11',flush=True)
                            return frame
                    print('Next-month button clicked but 2026.11 was not found.',flush=True)
                except Exception as exc:
                    print('Next-month click error:',type(exc).__name__,str(exc)[:160],flush=True)
    return None


async def inspect_calendar():
    """Read the Nov 7 date tile; unknown/unavailable must NEVER trigger an availability alert."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = await browser.new_context(locale='ko-KR', timezone_id='Asia/Seoul',
                                            viewport={'width': 1100, 'height': 950})
        page = await context.new_page()
        try:
            try:
                await page.goto(DIRECT_URL, wait_until='domcontentloaded', timeout=18000)
            except PlaywrightTimeoutError:
                print('Direct page loading timed out; checking partial page.',flush=True)
            await page.wait_for_timeout(1000)

            # If calendar isn't visible, click the booking tab once.
            if not any([await visible_month(f) in (10,11) for f in list(page.frames)]):
                for f in list(page.frames):
                    try:
                        tab = f.get_by_role('button',name=re.compile(r'^\s*(?:예매하기|예약하기)\s*$'))
                        if await tab.count() and await tab.first.is_visible():
                            await tab.first.click(timeout=3500)
                            break
                    except Exception:
                        pass
                await page.wait_for_timeout(900)

            november_frame = await navigate_to_november(context)
            result={'status':'unknown','reason':'Could not navigate to November calendar'}
            if november_frame is not None:
                await page.wait_for_timeout(550)
                result = await november_frame.evaluate(CALENDAR_JS)
            print('Nov 7 calendar result:',json.dumps(result,ensure_ascii=False)[:1800],flush=True)
            await diagnostic_calendar(context.pages[-1])
            return result['status']
        except Exception as exc:
            print('Calendar inspection error:',repr(exc),flush=True)
            return 'unknown'
        finally:
            await browser.close()


async def diagnostic_calendar(page):
    if os.environ.get('DEBUG_SCREENSHOT', '').lower() == 'true':
        Path('debug').mkdir(exist_ok=True)
        try:
            await page.screenshot(path='debug/calendar_page.png', timeout=10000, full_page=True)
        except Exception as exc:
            print('Calendar screenshot error:', type(exc).__name__, flush=True)
        records = []
        for i, frame in enumerate(page.frames):
            try:
                info = await frame.evaluate(r"""() => {
                    const t=(document.body?.innerText || '').slice(0,12000);
                    const fields=[...document.querySelectorAll('button,[role=button],a,[aria-label]')]
                      .filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0})
                      .map(e=>({tag:e.tagName,text:(e.innerText||'').trim().slice(0,90),
                         label:e.getAttribute('aria-label'), cls:String(e.className).slice(0,120)}))
                      .filter(x=>/2026|11|달|월|7|매진|예약|다음/.test(x.text+' '+x.label)).slice(0,75);
                    const dates=[...document.querySelectorAll('button.calendar_date')].map(e=>({
                      text:(e.innerText||'').trim().slice(0,70),className:String(e.className),
                      disabled:e.disabled,html:e.outerHTML.slice(0,420)}));
                    const month=(t.match(/(?:^|\n)\s*2026\.\s*(10|11)\s*(?:\n|$)/)||[])[1] || null;
                    return {title:document.title,detectedMonth:month,bodyText:t,controls:fields,
                            calendarDates:dates};
                }""")
                records.append({'frame':i,'url':frame.url, 'info':info})
            except Exception as exc:
                records.append({'frame':i, 'error':type(exc).__name__})
        Path('debug/calendar_dom.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Calendar DOM text diagnostic saved.', flush=True)


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
            cal_labels = {'open':'11/7 날짜 선택 가능','sold_out':'11/7 매진','unavailable':'11/7 선택 불가(매진 표시는 확인 안 됨)','unknown':'11/7 달력 판독 불가'}
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
