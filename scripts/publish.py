"""퍼블리셔 — 관문 PASS 게시물을 Instagram Graph API로 발행한다. (2주 차, 아직 실계정 미검증)

사용:
  python publish.py --date 2026-10-26            # 그날 id로 시작하는 게시물을 발행 (기본: 오늘)
  python publish.py --id <게시물id> --kind reel   # 특정 게시물·종류만
  python publish.py --dry-run                     # 실제 호출 없이 무엇을 할지만 출력 (기본값: 자격 증명이 없으면 자동 dry-run)

막는 장치 (사람 승인 대신):
  - P/KILL_SWITCH 파일이 있으면 즉시 종료
  - post.json gate.status가 PASS가 아니면 건너뜀 (check_post.py를 발행 직전에 다시 돌림 — 팩트체크 30일 만료 포함)
  - 같은 게시물·종류를 두 번 올리지 않음 (publish-log.csv)

자격 증명: 워크스페이스 밖 ~/.d280/credentials.json  (git에 들어가지 않게)
  {"ig_user_id": "...", "access_token": "...", "graph_version": "v23.0"}
이미지·영상 공개 URL: Graph API는 로컬 파일을 못 받는다. config.json의 "public_base_url" 아래에
  <id>/cards/01.png, <id>/reel.mp4 가 올라가 있어야 한다 (호스팅 방식은 계정 연결 때 정함).
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CRED_PATH = Path.home() / ".d280" / "credentials.json"
LOG_PATH = ROOT / "publish-log.csv"


def graph(cred, method, path, **params):
    # Instagram 로그인 토큰(get_token.py)은 graph.instagram.com, 페이스북 로그인 토큰은 graph.facebook.com
    url = f"https://{cred.get('api_host', 'graph.facebook.com')}/{cred.get('graph_version', 'v23.0')}/{path}"
    params["access_token"] = cred["access_token"]
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data.decode()}")
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # 오류 본문(사유)을 남긴다 — 토큰은 URL·본문에 넣지 않음
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {path}: {body}") from None


def wait_ready(cred, container_id, tries=30):
    for _ in range(tries):
        st = graph(cred, "GET", container_id, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st == "ERROR":
            raise RuntimeError(f"컨테이너 처리 실패: {container_id}")
        time.sleep(10)
    raise TimeoutError(f"컨테이너 대기 시간 초과: {container_id}")


def caption_of(post):
    return f"{post['caption']}\n\n{' '.join(post.get('hashtags', []))}".strip()


def url_of(base, post_id, rel):
    """공개 URL + 파일 내용 해시(?v=). 같은 주소로 다시 올리면 Instagram·CDN이 옛 이미지를 캐시에서 가져간다
    (2026-11-10 웹툰 재발행 때 빈 카드가 그대로 나간 사고) — 내용이 바뀌면 주소도 바뀌게 한다."""
    import hashlib
    h = hashlib.sha1((ROOT / "posts" / post_id / rel).read_bytes()).hexdigest()[:10]
    return f"{base}/{post_id}/{rel}?v={h}"


def publish_carousel(cred, post, base):
    pngs = sorted((ROOT / "posts" / post["id"] / "cards").glob("*.png"))
    children = [graph(cred, "POST", f"{cred['ig_user_id']}/media",
                      image_url=url_of(base, post["id"], f"cards/{p.name}"), is_carousel_item="true")["id"] for p in pngs]
    for c in children:
        wait_ready(cred, c)
    parent = graph(cred, "POST", f"{cred['ig_user_id']}/media", media_type="CAROUSEL",
                   children=",".join(children), caption=caption_of(post))["id"]
    wait_ready(cred, parent)
    return graph(cred, "POST", f"{cred['ig_user_id']}/media_publish", creation_id=parent)["id"]


def publish_reel(cred, post, base):
    extra = {}
    if (ROOT / "posts" / post["id"] / "reel-cover.png").exists():  # 피드 격자에서 캐러셀 표지와 겹치지 않게 색 타일 표지(make_reel_cover.py)
        extra["cover_url"] = url_of(base, post["id"], "reel-cover.png")
    cid = graph(cred, "POST", f"{cred['ig_user_id']}/media", media_type="REELS",
                video_url=url_of(base, post["id"], "reel.mp4"), caption=caption_of(post), share_to_feed="true", **extra)["id"]
    wait_ready(cred, cid)
    return graph(cred, "POST", f"{cred['ig_user_id']}/media_publish", creation_id=cid)["id"]


def publish_story(cred, post, base):
    # 저녁 스토리 — 표지 + 고정 안내 문구(make_story.py). 스토리는 캡션이 없다.
    cid = graph(cred, "POST", f"{cred['ig_user_id']}/media", media_type="STORIES",
                image_url=url_of(base, post["id"], "story.png"))["id"]
    wait_ready(cred, cid)
    return graph(cred, "POST", f"{cred['ig_user_id']}/media_publish", creation_id=cid)["id"]


PUBLISHERS = {"carousel": publish_carousel, "reel": publish_reel, "story": publish_story}


def already(post_id, kind):
    if not LOG_PATH.exists():
        return False
    with LOG_PATH.open(encoding="utf-8") as f:
        return any(r["id"] == post_id and r["kind"] == kind and r["result"] == "published" for r in csv.DictReader(f))


def log(post_id, kind, result, detail=""):
    new = not LOG_PATH.exists()
    with LOG_PATH.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "id", "kind", "result", "detail"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), post_id, kind, result, detail])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--id")
    # 하루 3번 (2026-11-10~): 08:30 carousel(매거진) / 12:30 reel(매거진) / 21:00 webtoon(웹툰 캐러셀). story는 수동 실행용으로만 남김
    ap.add_argument("--kind", choices=["carousel", "reel", "story", "webtoon", "both"], default="both")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if (ROOT / "KILL_SWITCH").exists():
        sys.exit("KILL_SWITCH가 있어 발행하지 않습니다.")
    cred = json.loads(CRED_PATH.read_text(encoding="utf-8")) if CRED_PATH.exists() else None
    if not cred and os.environ.get("IG_ACCESS_TOKEN"):  # GitHub Actions: 저장소 Secrets
        cred = {"ig_user_id": os.environ["IG_USER_ID"], "access_token": os.environ["IG_ACCESS_TOKEN"],
                "api_host": "graph.instagram.com", "graph_version": "v23.0"}
    base = CONFIG.get("public_base_url")
    dry = args.dry_run or not cred or not base
    if dry and not args.dry_run:
        print("자격 증명 또는 public_base_url이 없어 dry-run으로 실행합니다.")

    dirs = [ROOT / "posts" / args.id] if args.id else sorted((ROOT / "posts").glob(f"{args.date}-*"))
    kinds = ["carousel", "reel", "webtoon"] if args.kind == "both" else [args.kind]
    failed = False
    for d in dirs:
        # 발행 직전 관문 재검사 (팩트체크 만료·수정 반영)
        gate = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_post.py"), str(d)], capture_output=True, text=True, encoding="utf-8")
        post = json.loads((d / "post.json").read_text(encoding="utf-8"))
        if gate.returncode != 0:
            print(f"[HOLD] {post['id']} — 발행 안 함\n{gate.stdout}")
            log(post["id"], "-", "hold", gate.stdout.strip().replace("\n", " | ")[:300])
            continue
        webtoon = post.get("format") == "webtoon" or post.get("template") == "f-webtoon"
        for slot in kinds:
            # 웹툰은 저녁 webtoon 칸에서만 캐러셀로, 매거진은 아침 carousel·점심 reel·story 칸에서만 나간다
            if (slot == "webtoon") != webtoon:
                continue
            kind = "carousel" if slot == "webtoon" else slot
            if kind == "story" and not (d / "story.png").exists():
                print(f"[SKIP] {post['id']} story 이미지 없음")
                continue
            if already(post["id"], kind):
                print(f"[SKIP] {post['id']} {kind} 이미 발행됨")
                continue
            if dry:
                print(f"[DRY] {post['id']} {kind} 발행 예정")
                continue
            try:
                media_id = PUBLISHERS[kind](cred, post, base)
                log(post["id"], kind, "published", media_id)
                print(f"[OK] {post['id']} {kind} → {media_id}")
            except Exception as e:  # 조용히 멈추지 않는다 — 로그와 종료 코드로 알림
                failed = True
                log(post["id"], kind, "error", str(e)[:300])
                print(f"[ERROR] {post['id']} {kind}: {e}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
