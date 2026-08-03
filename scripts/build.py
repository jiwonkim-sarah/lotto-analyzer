"""로또 6/45 최근 5년치 통계 분석 + 추천 번호 5세트 생성.

표준 라이브러리만 사용한다 (GitHub Actions에서 의존성 설치 없이 돌리기 위해).
출력: docs/data.json

데이터 소스
  1차: https://smok95.github.io/lotto/results/all.json  (전 회차 미러)
  2차: 동행복권 공식 JSON API (미러에 아직 없는 최신 회차 보강용, 실패해도 무시)
"""

import json
import math
import random
import ssl
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data.json"

MIRROR_URL = "https://smok95.github.io/lotto/results/all.json"
OFFICIAL_URL = "https://www.dhlottery.co.kr/common.do?method=getLottoNumber&drwNo={}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

KST = timezone(timedelta(hours=9))
YEARS = 5
BALL_MIN, BALL_MAX = 1, 45
PICK = 6

BANDS = [(1, 10), (11, 20), (21, 30), (31, 40), (41, 45)]


# --------------------------------------------------------------------------- 수집


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_mirror():
    raw = json.loads(_get(MIRROR_URL))
    draws = []
    for row in raw:
        nums = row.get("numbers")
        if not nums or len(nums) != PICK:
            continue
        draws.append(
            {
                "no": int(row["draw_no"]),
                "date": str(row["date"])[:10],
                "numbers": sorted(int(n) for n in nums),
                "bonus": int(row["bonus_no"]),
            }
        )
    draws.sort(key=lambda d: d["no"])
    return draws


def fetch_official(draw_no):
    """공식 API로 단일 회차 조회. 대기열/차단 시 None."""
    try:
        body = _get(OFFICIAL_URL.format(draw_no), timeout=20).lstrip()
        if not body.startswith("{"):
            return None
        row = json.loads(body)
        if row.get("returnValue") != "success":
            return None
        nums = sorted(int(row[f"drwtNo{i}"]) for i in range(1, 7))
        return {
            "no": int(row["drwNo"]),
            "date": str(row["drwNoDate"])[:10],
            "numbers": nums,
            "bonus": int(row["bnusNo"]),
        }
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError, OSError):
        return None


def collect():
    draws = fetch_mirror()
    if not draws:
        raise SystemExit("데이터 수집 실패: 미러에서 회차를 하나도 받지 못했습니다.")

    # 미러가 아직 반영하지 못한 최신 회차를 공식 API로 최대 3회 보강 시도.
    for _ in range(3):
        nxt = fetch_official(draws[-1]["no"] + 1)
        if not nxt:
            break
        draws.append(nxt)
        print(f"공식 API로 {nxt['no']}회 보강")

    return draws


# --------------------------------------------------------------------------- 분석


def band_of(n):
    for lo, hi in BANDS:
        if lo <= n <= hi:
            return f"{lo}–{hi}"
    return "?"


def analyse(draws):
    latest = draws[-1]
    latest_date = datetime.strptime(latest["date"], "%Y-%m-%d").replace(tzinfo=KST)
    cutoff = latest_date - timedelta(days=365 * YEARS + YEARS // 4)

    window = [d for d in draws if datetime.strptime(d["date"], "%Y-%m-%d").replace(tzinfo=KST) >= cutoff]
    n_draws = len(window)

    freq = Counter()
    for d in window:
        freq.update(d["numbers"])

    # 미출현 간격은 전체 이력 기준으로 정확히 계산한다.
    last_seen = {}
    for d in draws:
        for n in d["numbers"]:
            last_seen[n] = d["no"]

    expected = n_draws * PICK / (BALL_MAX - BALL_MIN + 1)
    frequency = []
    for n in range(BALL_MIN, BALL_MAX + 1):
        c = freq.get(n, 0)
        frequency.append(
            {
                "n": n,
                "count": c,
                "pct": round(c / (n_draws * PICK) * 100, 2) if n_draws else 0.0,
                "vs_expected": round(c - expected, 1),
                "gap": latest["no"] - last_seen[n] if n in last_seen else None,
                "band": band_of(n),
            }
        )

    ranked = sorted(frequency, key=lambda r: (-r["count"], r["n"]))
    hot = [r["n"] for r in ranked[:6]]
    cold = [r["n"] for r in ranked[-6:]]
    overdue = [r["n"] for r in sorted(frequency, key=lambda r: (-(r["gap"] or 0), r["n"]))[:6]]

    # 궁합수 (동반 출현)
    pairs = Counter()
    for d in window:
        ns = d["numbers"]
        for i in range(PICK):
            for j in range(i + 1, PICK):
                pairs[(ns[i], ns[j])] += 1
    top_pairs = [{"a": a, "b": b, "count": c} for (a, b), c in pairs.most_common(12)]

    # 합계 분포 (20단위 구간)
    sums = [sum(d["numbers"]) for d in window]
    sum_bins = Counter()
    for s in sums:
        sum_bins[((s - 1) // 20) * 20 + 1] += 1
    sum_hist = [
        {"label": f"{lo}–{lo + 19}", "lo": lo, "count": sum_bins.get(lo, 0)}
        for lo in range(1, 261, 20)
        if sum_bins.get(lo, 0) or 61 <= lo <= 221
    ]

    # 홀수 개수 분포
    odd_counts = Counter(sum(1 for n in d["numbers"] if n % 2) for d in window)
    odd_hist = [{"odd": k, "count": odd_counts.get(k, 0)} for k in range(PICK + 1)]

    # 구간 분포 (구간 크기가 다르므로 기대치와 함께 낸다)
    band_counts = Counter()
    for d in window:
        for n in d["numbers"]:
            band_counts[band_of(n)] += 1
    total_balls = n_draws * PICK
    band_dist = []
    for lo, hi in BANDS:
        label = f"{lo}–{hi}"
        c = band_counts.get(label, 0)
        size = hi - lo + 1
        band_dist.append(
            {
                "label": label,
                "count": c,
                "pct": round(c / total_balls * 100, 2) if total_balls else 0.0,
                "expected_pct": round(size / 45 * 100, 2),
            }
        )

    # 연속번호 포함 회차 비율
    consec = sum(
        1 for d in window if any(d["numbers"][i + 1] - d["numbers"][i] == 1 for i in range(PICK - 1))
    )

    return {
        "window": window,
        "n_draws": n_draws,
        "frequency": frequency,
        "hot": hot,
        "cold": cold,
        "overdue": overdue,
        "top_pairs": top_pairs,
        "sum_hist": sum_hist,
        "odd_hist": odd_hist,
        "band_dist": band_dist,
        "sum_mean": round(sum(sums) / len(sums), 1) if sums else 0,
        "sum_p10": percentile(sums, 10),
        "sum_p90": percentile(sums, 90),
        "consecutive_rate": round(consec / n_draws * 100, 1) if n_draws else 0.0,
        "pairs": pairs,
    }


def percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[lo]
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo))


# --------------------------------------------------------------------------- 추천


def weighted_sample(rng, weights, k):
    """복원 없는 가중 추출 (weights: {번호: 가중치})."""
    pool = dict(weights)
    picked = []
    while len(picked) < k and pool:
        total = sum(pool.values())
        if total <= 0:
            picked.extend(rng.sample(list(pool), k - len(picked)))
            break
        r = rng.random() * total
        acc = 0.0
        for n, w in pool.items():
            acc += w
            if acc >= r:
                picked.append(n)
                del pool[n]
                break
        else:
            n = next(iter(pool))
            picked.append(n)
            del pool[n]
    return sorted(picked)


def passes_filters(nums, lo_sum, hi_sum):
    """실제 당첨 조합이 대부분 만족하는 통계 필터."""
    s = sum(nums)
    if not lo_sum <= s <= hi_sum:
        return False
    odd = sum(1 for n in nums if n % 2)
    if not 2 <= odd <= 4:
        return False
    # 3연속 이상 배제
    run = 1
    for i in range(len(nums) - 1):
        run = run + 1 if nums[i + 1] - nums[i] == 1 else 1
        if run >= 3:
            return False
    # 최소 3개 구간에 걸치기
    if len({band_of(n) for n in nums}) < 3:
        return False
    # 같은 끝자리 3개 이상 배제
    if max(Counter(n % 10 for n in nums).values()) >= 3:
        return False
    return True


def recommend(stats, seed):
    """최신 회차 번호를 시드로 쓰는 결정론적 추천 — 회차가 바뀌면 번호도 바뀐다."""
    freq = {r["n"]: r["count"] for r in stats["frequency"]}
    gaps = {r["n"]: (r["gap"] or 0) for r in stats["frequency"]}
    lo_sum, hi_sum = stats["sum_p10"], stats["sum_p90"]
    fmax = max(freq.values()) or 1
    gmax = max(gaps.values()) or 1

    def build(label, tag, desc, weights, rng, seeds=()):
        for _ in range(4000):
            picked = list(seeds) + weighted_sample(
                rng, {n: w for n, w in weights.items() if n not in seeds}, PICK - len(seeds)
            )
            picked = sorted(set(picked))
            if len(picked) == PICK and passes_filters(picked, lo_sum, hi_sum):
                break
        else:  # 필터를 못 맞추면 필터 없이 마지막 결과를 쓴다
            picked = sorted(set(list(seeds) + weighted_sample(rng, weights, PICK - len(seeds))))[:PICK]
        return {
            "label": label,
            "tag": tag,
            "desc": desc,
            "numbers": picked,
            "sum": sum(picked),
            "odd": sum(1 for n in picked if n % 2),
            "bands": len({band_of(n) for n in picked}),
        }

    out = []

    # 1) 핫 넘버 — 최근 5년 출현 빈도에 비례 (제곱 가중으로 상위 편향)
    rng = random.Random(seed * 31 + 1)
    w = {n: (freq[n] / fmax) ** 2.2 + 0.02 for n in freq}
    out.append(build("핫 넘버", "hot", f"최근 5년 출현 빈도 상위 번호에 가중치를 둔 조합", w, rng))

    # 2) 콜드 / 미출현 — 오래 안 나온 번호 우선
    rng = random.Random(seed * 31 + 2)
    w = {n: (gaps[n] / gmax) ** 1.8 + 0.02 for n in freq}
    out.append(build("장기 미출현", "cold", "가장 오랫동안 나오지 않은 번호에 가중치를 둔 조합", w, rng))

    # 3) 균형 — 모든 번호 동일 가중 + 구간 5개 전부 커버
    rng = random.Random(seed * 31 + 3)
    balanced = None
    for _ in range(6000):
        cand = sorted(rng.sample(range(BALL_MIN, BALL_MAX + 1), PICK))
        if passes_filters(cand, lo_sum, hi_sum) and len({band_of(n) for n in cand}) >= 4:
            odd = sum(1 for n in cand if n % 2)
            if odd == 3:
                balanced = cand
                break
    if balanced is None:
        balanced = sorted(rng.sample(range(BALL_MIN, BALL_MAX + 1), PICK))
    out.append(
        {
            "label": "균형 배분",
            "tag": "balanced",
            "desc": "홀짝 3:3, 4개 이상 구간에 분산, 합계는 최빈 구간 안에 드는 조합",
            "numbers": balanced,
            "sum": sum(balanced),
            "odd": sum(1 for n in balanced if n % 2),
            "bands": len({band_of(n) for n in balanced}),
        }
    )

    # 4) 궁합수 — 동반 출현이 잦은 쌍을 씨앗으로 확장
    rng = random.Random(seed * 31 + 4)
    pairs = stats["pairs"]
    top = [p for p, _ in pairs.most_common(10)]
    a, b = top[seed % len(top)] if top else (1, 2)
    partner = Counter()
    for (x, y), c in pairs.items():
        if x in (a, b):
            partner[y] += c
        if y in (a, b):
            partner[x] += c
    pmax = max(partner.values()) if partner else 1
    w = {n: (partner.get(n, 0) / pmax) ** 1.6 + 0.02 for n in freq}
    out.append(
        build(
            "궁합수",
            "pair",
            f"최근 5년 동반 출현이 가장 잦았던 {a}·{b}을(를) 축으로 확장한 조합",
            w,
            rng,
            seeds=(a, b),
        )
    )

    # 5) 통계 필터 랜덤 — 완전 무작위 후 통계 필터만 통과
    rng = random.Random(seed * 31 + 5)
    pick = None
    for _ in range(6000):
        cand = sorted(rng.sample(range(BALL_MIN, BALL_MAX + 1), PICK))
        if passes_filters(cand, lo_sum, hi_sum):
            pick = cand
            break
    if pick is None:
        pick = sorted(rng.sample(range(BALL_MIN, BALL_MAX + 1), PICK))
    out.append(
        {
            "label": "필터 랜덤",
            "tag": "random",
            "desc": "무작위 추출 후 합계·홀짝·연속·끝자리 통계 필터를 통과한 조합",
            "numbers": pick,
            "sum": sum(pick),
            "odd": sum(1 for n in pick if n % 2),
            "bands": len({band_of(n) for n in pick}),
        }
    )

    return out


# --------------------------------------------------------------------------- 실행


def main():
    draws = collect()
    stats = analyse(draws)
    latest = draws[-1]
    window = stats["window"]

    recs = recommend(stats, latest["no"])

    payload = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "source": "smok95/lotto (동행복권 미러) + 동행복권 공식 API",
        "latest": latest,
        "next_draw_no": latest["no"] + 1,
        "window": {
            "years": YEARS,
            "draw_count": stats["n_draws"],
            "from_draw": window[0]["no"],
            "to_draw": window[-1]["no"],
            "from_date": window[0]["date"],
            "to_date": window[-1]["date"],
        },
        "totals": {"all_draws": len(draws)},
        "recommendations": recs,
        "frequency": stats["frequency"],
        "hot": stats["hot"],
        "cold": stats["cold"],
        "overdue": stats["overdue"],
        "top_pairs": stats["top_pairs"],
        "sum_hist": stats["sum_hist"],
        "odd_hist": stats["odd_hist"],
        "band_dist": stats["band_dist"],
        "sum_mean": stats["sum_mean"],
        "sum_p10": stats["sum_p10"],
        "sum_p90": stats["sum_p90"],
        "consecutive_rate": stats["consecutive_rate"],
        "recent": list(reversed(window[-10:])),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    OUT.write_text(body, encoding="utf-8")
    # file:// 로 열어도 동작하도록 스크립트 형태로도 내보낸다 (fetch/CORS 회피).
    (OUT.parent / "data.js").write_text(f"window.LOTTO_DATA = {body};\n", encoding="utf-8")

    print(f"전체 {len(draws)}회 수집, 분석 윈도우 {stats['n_draws']}회 "
          f"({window[0]['no']}~{window[-1]['no']}회, {window[0]['date']}~{window[-1]['date']})")
    print(f"최신 회차 {latest['no']}회 {latest['date']} {latest['numbers']} + {latest['bonus']}")
    for r in recs:
        print(f"  [{r['label']}] {r['numbers']}  합계 {r['sum']}, 홀 {r['odd']}, 구간 {r['bands']}")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
