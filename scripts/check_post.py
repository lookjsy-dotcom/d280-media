"""발행 관문 — 사람 승인 대신 기계가 막는 자리.

post.json 하나를 검사해 PASS / HOLD를 판정하고 post.json의 gate 필드에 기록한다.
HOLD면 퍼블리셔가 발행하지 않는다. 종료 코드: PASS 0, HOLD 1.

사용: python check_post.py <posts/게시물폴더> [--no-write]

검사 항목 (progress.md "합격 기준"의 게시물 단위를 기계로 확인 가능한 것만):
  - 카드 수, 표지 제목 길이, 훅 유형, 마지막 카드가 closing인지
  - 카드에 나온 모든 숫자가 claims 중 하나에 들어 있는지 (출처 없는 수치 0개)
  - 모든 claim이 factcheck에서 OK/FIXED + 공식 도메인 출처 + 확인일이 최근인지
  - 금지 표현
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
HOOK_TYPES = {"손실회피", "마감", "통념뒤집기"}
# 숫자 덩어리: 1,000 / 2.5 / 100만 / 60일 같은 것. 단위는 비교에서 빼고 숫자만 본다.
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def card_texts(card):
    parts = [card.get(k, "") for k in ("kicker", "title", "sub", "body", "tip")]
    parts += card.get("items", []) or []
    return [p for p in parts if p]


def norm_num(s):
    return s.replace(",", "")


def is_official(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d.lstrip(".") or host.endswith(d if d.startswith(".") else "." + d) for d in CONFIG["official_domains"])


def check(post):
    problems = []
    cards = post.get("cards", [])
    lo, hi = CONFIG["card_count"]
    if not lo <= len(cards) <= hi:
        problems.append(f"카드 수 {len(cards)}장 (허용 {lo}~{hi}장)")
    if not cards or cards[0].get("type") != "cover":
        problems.append("첫 카드가 cover가 아님")
    else:
        title = cards[0].get("title", "").replace("\n", "")
        if len(title) > CONFIG["cover_title_max"]:
            problems.append(f"표지 제목 {len(title)}자 (최대 {CONFIG['cover_title_max']}자): {title}")
    if not cards or cards[-1].get("type") != "closing":
        problems.append("마지막 카드가 closing(기준일 카드)이 아님")
    if post.get("hook_type") not in HOOK_TYPES:
        problems.append(f"hook_type이 {sorted(HOOK_TYPES)} 중 하나가 아님: {post.get('hook_type')}")
    if not post.get("base_date"):
        problems.append("base_date(정보 기준일) 없음")

    # 금지 표현
    all_text = " ".join(t for c in cards for t in card_texts(c)) + " " + post.get("caption", "")
    for w in CONFIG["banned_words"]:
        if w in all_text:
            problems.append(f"금지 표현 사용: '{w}'")

    # 카드 숫자 → claims 커버리지
    claims = post.get("claims", [])
    claim_nums = set()
    for cl in claims:
        for n in NUM_RE.findall(cl.get("text", "") + " " + cl.get("value", "")):
            claim_nums.add(norm_num(n))
    for i, c in enumerate(cards):
        for t in card_texts(c):
            for n in NUM_RE.findall(t):
                if norm_num(n) not in claim_nums:
                    problems.append(f"카드 {i + 1}의 숫자 '{n}'가 claims에 없음 (출처 없는 수치): {t[:40]}")

    # 팩트체크 결과
    fc = post.get("factcheck") or {}
    results = {r.get("id"): r for r in fc.get("claims", [])}
    max_age = CONFIG["factcheck_max_age_days"]
    for cl in claims:
        r = results.get(cl.get("id"))
        if not r:
            problems.append(f"claim {cl.get('id')} 팩트체크 기록 없음: {cl.get('text', '')[:40]}")
            continue
        if r.get("verdict") not in ("OK", "FIXED"):
            problems.append(f"claim {cl['id']} 판정 {r.get('verdict')}: {cl.get('text', '')[:40]}")
        if not is_official(r.get("source_url", "")):
            problems.append(f"claim {cl['id']} 출처가 공식 도메인이 아님: {r.get('source_url')}")
        try:
            age = (date.today() - datetime.strptime(r.get("checked_at", ""), "%Y-%m-%d").date()).days
            if age > max_age:
                problems.append(f"claim {cl['id']} 확인일이 {age}일 전 (최대 {max_age}일) — 재검사 필요")
        except ValueError:
            problems.append(f"claim {cl['id']} 확인일(checked_at) 형식 오류: {r.get('checked_at')}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("post_dir")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    path = Path(args.post_dir) / "post.json"
    post = json.loads(path.read_text(encoding="utf-8"))
    problems = check(post)
    status = "PASS" if not problems else "HOLD"
    print(f"[{status}] {post.get('id')}")
    for p in problems:
        print(f"  - {p}")
    if not args.no_write:
        post["gate"] = {"status": status, "checked_at": date.today().isoformat(), "problems": problems}
        path.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(0 if status == "PASS" else 1)


if __name__ == "__main__":
    main()
