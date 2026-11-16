"""Blue Note Tokyo 좌석 모니터 → 카카오톡 '나에게 보내기'.

대상: SOIL&"PIMP"SESSIONS YEAR-END SPECIAL LIVE 2026 (exec/4015), 12/5(토) 1st / 2nd.
좌석 상태가 '×(만석)'이 아니게 되면(○ 또는 △) 카카오톡으로 알림을 보낸다.

환경변수:
  KAKAO_REST_API_KEY      (필수) 카카오 디벤로퍼스 REST API 키
  KAKAO_REFRESH_TOKEN     (필수) kakao_setup.py 로 발급한 refresh token
  KAKAO_CLIENT_SECRET     (선택) 앱에서 Client Secret을 켰다면 입력
  NTFY_TOPIC              (선택) ntfy 토픽 이름 → 폰 푸시
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  (선택) 텔레그램 봇 알림
  DRY_RUN=1               카톡을 보내지 않고 결과만 출력 (테스트용)
  FORCE_NOTIFY=1          상태와 상관없이 테스트 메시지 1통 발송 (카톡 연동 점검용)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

LIST_URL = "https://reserve.bluenote.co.jp/reserve/schedule/move/202612"
EVENT_HREF = "/reserve/schedule/exec/4015"
TARGET_DATE_PATTERNS = [r"12\.\s*5\b", r"December\s+5\b", r"12月\s*5日"]  # 날짜 제목 매칭
TARGET_SHOWS = ["1st", "2nd"]

# 날짜 블록 → 공연 목록을 읽어 {show: (icon_signature, text)}와 범례 3종 아이콘 시그니처를 돌려주는 JS
EXTRACT_JS = r"""
(datePatterns) => {
  const dialog = document.querySelector('[role="dialog"]') || document.body;
  const sig = (el) => {
    if (!el) return '';
    const img = el.matches('img') ? el : el.querySelector('img');
    if (img) return 'img:' + (img.getAttribute('src') || '') ;
    const svg = el.matches('svg') ? el : el.querySelector('svg');
    if (svg) return 'svg:' + svg.innerHTML.replace(/\s+/g, ' ').trim();
    return 'cls:' + (el.className || '');
  };
  // 1) 범례: 아이콘 + 텍스트 3개 (○ / △ / ×)
  const legendLabels = [
    /Seats available|空席あり/i, /Only a few left|残りわずか/i, /No seats available|空席なし/i,
  ];
  const legend = {};
  const all = Array.from(dialog.querySelectorAll('*'));
  legendLabels.forEach((re, i) => {
    const textEl = all.find(e => e.children.length === 0 && re.test((e.textContent || '').trim()));
    if (textEl) {
      const box = textEl.parentElement;
      legend[i] = sig(box.querySelector('img, svg') || box);
    }
  });
  // 2) 대상 날짜 제목 찾기
  const res = new RegExp(datePatterns.join('|'));
  const dateEl = all.find(e => e.children.length === 0 && res.test((e.textContent || '').trim()) &&
                               e.tagName !== 'SCRIPT');
  if (!dateEl) return {error: 'date heading not found', legend};
  // 날짜 제목과 같은 컨테이너 안의 공연 항목(1st/2nd)을 찾는다
  let container = dateEl.parentElement;
  let shows = {};
  for (let depth = 0; depth < 4 && container; depth++, container = container.parentElement) {
    const found = {};
    Array.from(container.querySelectorAll('*')).forEach(e => {
      if (e.children.length === 0) {
        const t = (e.textContent || '').trim();
        if (t === '1st' || t === '2nd') {
          // 항목 박스: 아이콘(img/svg)을 포함하는 가장 가까운 조상
          let box = e.parentElement;
          while (box && !box.querySelector('img, svg') && box !== container) box = box.parentElement;
          if (box) found[t] = {icon: sig(box.querySelector('img, svg')), text: (box.innerText || '').replace(/\s+/g, ' ').trim(), cls: String(box.className || '')};
        }
      }
    });
    if (found['1st'] && found['2nd']) { shows = found; break; }
  }
  return {shows, legend};
}
"""


def fetch_status():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="en-US",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        )
        page = ctx.new_page()
        try:
            page.goto(LIST_URL, wait_until="domcontentloaded", timeout=45000)
            link = page.locator(f'a[href$="{EVENT_HREF}"]:visible').first
            link.wait_for(state="visible", timeout=30000)
            link.click()
            # 팝업 안의 공연 회차(1st) 가 화면에 나타날 때까지 대기 (role 에 의존하지 않음)
            page.get_by_text("1st", exact=True).first.wait_for(state="visible", timeout=30000)
            page.wait_for_timeout(1000)  # 아이콘 렌더링 여유
            data = page.evaluate(EXTRACT_JS, TARGET_DATE_PATTERNS)
            return data
        except Exception:
            # 원인 파악용: 현재 페이지 상태를 로그에 남긴다
            print("---- DEBUG url:", page.url)
            try:
                print("---- DEBUG title:", page.title())
                print("---- DEBUG body text (앞 2500자):")
                print(page.inner_text("body")[:2500])
            except Exception as e2:
                print("---- DEBUG body read failed:", repr(e2))
            raise
        finally:
            browser.close()


def classify(data):
    """각 공연을 'full' / 'few' / 'open' / 'unknown' 으로 분류."""
    if data.get("error"):
        raise RuntimeError(data["error"])
    legend = data.get("legend", {})
    full_sig = legend.get("2") or legend.get(2)
    open_sig = legend.get("0") or legend.get(0)
    few_sig = legend.get("1") or legend.get(1)
    if not full_sig:
        raise RuntimeError("legend (×) icon not found - page layout may have changed")
    out = {}
    for name in TARGET_SHOWS:
        show = data["shows"].get(name)
        if not show:
            raise RuntimeError(f"{name} entry not found")
        s = show["icon"]
        if s == full_sig:
            out[name] = "full"
        elif s == open_sig:
            out[name] = "open"
        elif s == few_sig:
            out[name] = "few"
        else:
            out[name] = "unknown"
    return out


def kakao_access_token():
    body = {
        "grant_type": "refresh_token",
        "client_id": os.environ["KAKAO_REST_API_KEY"],
        "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"],
    }
    if os.environ.get("KAKAO_CLIENT_SECRET"):
        body["client_secret"] = os.environ["KAKAO_CLIENT_SECRET"]
    req = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=urllib.parse.urlencode(body).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["access_token"]


def kakao_send(text):
    token = kakao_access_token()
    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": LIST_URL, "mobile_web_url": LIST_URL},
        "button_title": "예약 페이지 열기",
    }
    req = urllib.request.Request(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        data=urllib.parse.urlencode({"template_object": json.dumps(template, ensure_ascii=False)}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        print("kakao send:", r.status, r.read().decode())


def kakao_calendar_send(text):
    """톡캘린더에 '곧 시작하는 일정'을 만들어 카카오톡 알림(푸시)을 유도한다.
    - 시작/종료 시각은 5분 단위여야 한다 (카카오 제약)
    - 일정 시작 5~10분 전 알림 + 시작 시점 알림을 건다 (0 이 거부되면 [5] 로 재시도)
    - 토큰에 talk_calendar 동의가 포함되어 있어야 한다
    """
    token = kakao_access_token()
    now = datetime.now(timezone.utc)
    start = (now + timedelta(minutes=5)).replace(second=0, microsecond=0)
    start += timedelta(minutes=(-start.minute) % 5)
    end = start + timedelta(minutes=5)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    last_err = None
    for reminders in ([0, 5], [5]):
        event = {
            "title": "🎷 블루노트 12/5 자리 났어요!",
            "time": {"start_at": start.strftime(fmt), "end_at": end.strftime(fmt),
                     "time_zone": "Asia/Seoul", "all_day": False, "lunar": False},
            "description": text + "\n" + LIST_URL,
            "reminders": reminders,
            "color": "RED",
        }
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/api/calendar/create/event",
            data=urllib.parse.urlencode({"event": json.dumps(event, ensure_ascii=False)}).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                print("kakao calendar send:", r.status, r.read().decode(), "reminders=", reminders)
                return
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            print(f"kakao calendar HTTP {e.code} (reminders={reminders}): {body}", file=sys.stderr)
            last_err = e
    raise last_err


def ntfy_send(text):
    topic = os.environ["NTFY_TOPIC"]
    payload = {
        "topic": topic,
        "title": "블루노트 12/5 자리 알림",
        "message": text,
        "priority": 5,
        "tags": ["saxophone"],
        "click": LIST_URL,
    }
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        print("ntfy send:", r.status)


def telegram_send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    body = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": text + "\n" + LIST_URL,
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=20) as r:
        print("telegram send:", r.status)


def notify_all(text):
    """설정된 모든 채널로 전송. 하나라도 성공하면 OK, 전부 실패하면 예외."""
    channels = []
    if os.environ.get("KAKAO_REST_API_KEY") and os.environ.get("KAKAO_REFRESH_TOKEN"):
        channels.append(("kakao", kakao_send))
        channels.append(("kakao-calendar", kakao_calendar_send))
    if os.environ.get("NTFY_TOPIC"):
        channels.append(("ntfy", ntfy_send))
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        channels.append(("telegram", telegram_send))
    if not channels:
        raise RuntimeError("no notification channel configured")
    ok = 0
    for name, fn in channels:
        try:
            fn(text)
            ok += 1
        except Exception as e:
            print(f"notify {name} FAILED:", repr(e), file=sys.stderr)
    if ok == 0:
        raise RuntimeError("all notification channels failed")


LABEL = {"full": "×(만석)", "few": "△(잔여 적음)", "open": "○(좌석 있음)", "unknown": "?(판별 불가)"}


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    force = os.environ.get("FORCE_NOTIFY") == "1"

    status = classify(fetch_status())
    print("status:", {k: LABEL[v] for k, v in status.items()})

    # '×' 가 아닌 모든 상태(○, △, 판별 불가)는 알림 대상 — 놓치는 것보다 오탐이 낫다
    available = [k for k, v in status.items() if v != "full"]

    if force:
        msg = "[테스트] 블루노트 모니터 연동 확인. 현재 상태: " + ", ".join(f"{k} {LABEL[v]}" for k, v in status.items())
    elif available:
        msg = ("🎷 12/5(토) SOIL&PIMP 자리 생김! "
               + ", ".join(f"{k} {LABEL[status[k]]}" for k in available)
               + "\n지금 바로 예약 페이지 확인하세요.")
    else:
        return 0

    print("message:", msg)
    if dry:
        print("(DRY_RUN) 전송 생략")
    else:
        notify_all(msg)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 실패는 워크플로 실패(→ GitHub 메일)로 드러나게 한다
        print("ERROR:", repr(e), file=sys.stderr)
        sys.exit(1)
