# utils/stats.py
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timedelta
import json
import os

# 데이터 디렉토리 생성
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

STATS_PATH = DATA_DIR / "user_stats.json"
MANG_PATH = DATA_DIR / "mang.json"  # 기존 'mang.json'도 같은 폴더로

# 유저 기본 레코드
DEFAULT_USER = {
    "참여": 0,
    "승리": 0,
    "패배": 0,
    "포인트": 0,
    "경험치": 0,
    "출석_마지막": None,  # "YYYY-MM-DD"
}

def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except UnicodeDecodeError:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def _write_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_stats() -> dict:
    return _read_json(STATS_PATH)

def save_stats(data: dict) -> None:
    _write_json(STATS_PATH, data)

def ensure_user(stats: dict, uid: str) -> dict:
    """해당 유저 레코드를 보장하고 누락 키를 채움."""
    rec = stats.get(uid)
    if rec is None:
        rec = DEFAULT_USER.copy()
        stats[uid] = rec
    else:
        for k, v in DEFAULT_USER.items():
            rec.setdefault(k, v)
    return rec

def format_num(n: int | float) -> str:
    return f"{n:,}"

def update_result_dual(user_id: str, won: bool) -> None:
    """
    내전/멸망(스크림) 결과를 양쪽 파일(user_stats.json, mang.json)에 업데이트.
    새 스키마(포인트/경험치/출석)도 자동 보강.
    """
    for path in (STATS_PATH, MANG_PATH):
        stats = _read_json(path)
        if user_id not in stats:
            stats[user_id] = DEFAULT_USER.copy()
        else:
            for k, v in DEFAULT_USER.items():
                stats[user_id].setdefault(k, v)

        stats[user_id]["참여"] += 1
        if won:
            stats[user_id]["승리"] += 1
        else:
            stats[user_id]["패배"] += 1

        _write_json(path, stats)

# --- points helpers ---
def get_points(user_id: int | str) -> int:
    stats = load_stats()
    rec = ensure_user(stats, str(user_id))
    return int(rec.get("포인트", 0))

def add_points(user_id: int | str, amount: int) -> int:
    """양수/음수 모두 허용. 음수면 차감, 최소 0 보장."""
    stats = load_stats()
    rec = ensure_user(stats, str(user_id))
    rec["포인트"] = max(0, int(rec.get("포인트", 0)) + int(amount))
    save_stats(stats)
    return rec["포인트"]

def can_spend_points(user_id: int | str, amount: int) -> bool:
    return get_points(user_id) >= int(amount)

def spend_points(user_id: int | str, amount: int) -> bool:
    """성공 시 True, 잔액 부족이면 False"""
    amount = int(amount)
    stats = load_stats()
    rec = ensure_user(stats, str(user_id))
    if rec.get("포인트", 0) < amount:
        return False
    rec["포인트"] = int(rec.get("포인트", 0)) - amount
    save_stats(stats)
    return True