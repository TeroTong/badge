from __future__ import annotations

import asyncio
import csv
import json
import time
from pathlib import Path
from typing import Any

import httpx

from smart_badge_api.dingtalk import get_access_token

BASE = "https://oapi.dingtalk.com"
STAMP = time.strftime("%Y%m%d_%H%M%S")
OUT_DIR = Path("/opt/badge/exports")
OUT_JSON = OUT_DIR / f"dingtalk_users_{STAMP}.json"
OUT_CSV = OUT_DIR / f"dingtalk_users_{STAMP}.csv"
OUT_TXT = OUT_DIR / f"dingtalk_userids_{STAMP}.txt"
DEPT_CONCURRENCY = 16
USER_CONCURRENCY = 8


def _text(value: Any) -> str:
    return str(value or "").strip()


async def post(
    client: httpx.AsyncClient,
    path: str,
    token: str,
    body: dict[str, Any],
    *,
    tries: int = 4,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(tries):
        try:
            resp = await client.post(f"{BASE}{path}", params={"access_token": token}, json=body)
            payload = resp.json()
            if payload.get("errcode") == 0:
                return payload
            last_error = RuntimeError(f"{path} failed: {json.dumps(payload, ensure_ascii=False)}")
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(0.25 * (attempt + 1))
    raise last_error or RuntimeError(f"{path} failed")


async def collect_departments(client: httpx.AsyncClient, token: str) -> list[int]:
    seen: set[int] = set()
    queue: list[int] = [1]
    ordered: list[int] = []
    while queue:
        batch: list[int] = []
        while queue and len(batch) < DEPT_CONCURRENCY:
            dept_id = int(queue.pop(0))
            if dept_id in seen:
                continue
            seen.add(dept_id)
            ordered.append(dept_id)
            batch.append(dept_id)
        if not batch:
            continue

        async def one(dept_id: int) -> tuple[int, list[int], str | None]:
            try:
                data = await post(client, "/topapi/v2/department/listsubid", token, {"dept_id": dept_id})
                children = [int(item) for item in (data.get("result") or {}).get("dept_id_list") or []]
                return dept_id, children, None
            except Exception as exc:
                return dept_id, [], str(exc)

        results = await asyncio.gather(*(one(dept_id) for dept_id in batch))
        for dept_id, children, error in results:
            if error:
                print(
                    json.dumps(
                        {"stage": "department_error", "dept_id": dept_id, "error": error},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            for child in children:
                if child not in seen and child not in queue:
                    queue.append(child)
        if len(ordered) % 200 < len(batch):
            print(
                json.dumps({"stage": "departments", "count": len(ordered), "queue": len(queue)}, ensure_ascii=False),
                flush=True,
            )
    return ordered


def merge_user(users: dict[str, dict[str, Any]], row: dict[str, Any], seen_dept_id: int) -> None:
    userid = _text(row.get("userid"))
    if not userid:
        return
    current = users.setdefault(userid, {"userid": userid, "seen_dept_ids": []})
    if seen_dept_id not in current["seen_dept_ids"]:
        current["seen_dept_ids"].append(seen_dept_id)
    for key in [
        "name",
        "job_number",
        "title",
        "mobile",
        "active",
        "dept_id_list",
        "unionid",
        "email",
        "org_email",
        "remark",
        "state_code",
        "telephone",
        "work_place",
        "extension",
        "exclusive_account",
        "manager_userid",
    ]:
        value = row.get(key)
        if value in (None, "", []):
            continue
        if current.get(key) in (None, "", []):
            current[key] = value
        elif key == "dept_id_list" and isinstance(value, list):
            current[key] = list(dict.fromkeys([*(current.get(key) or []), *value]))


async def collect_users(
    client: httpx.AsyncClient,
    token: str,
    departments: list[int],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    users: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(USER_CONCURRENCY)
    completed = 0

    async def list_dept_users(dept_id: int) -> None:
        nonlocal completed
        cursor = 0
        async with sem:
            while True:
                try:
                    data = await post(
                        client,
                        "/topapi/v2/user/list",
                        token,
                        {
                            "dept_id": dept_id,
                            "cursor": cursor,
                            "size": 100,
                            "contain_access_limit": True,
                            "language": "zh_CN",
                        },
                    )
                except Exception as exc:
                    errors.append({"dept_id": dept_id, "cursor": cursor, "error": str(exc)})
                    break
                result = data.get("result") or {}
                for row in result.get("list") or []:
                    if isinstance(row, dict):
                        merge_user(users, row, dept_id)
                if result.get("has_more"):
                    cursor = int(result.get("next_cursor") or 0)
                else:
                    break
        completed += 1
        if completed % 200 == 0 or completed == len(departments):
            print(
                json.dumps(
                    {
                        "stage": "users",
                        "departments_done": completed,
                        "departments_total": len(departments),
                        "unique_users": len(users),
                        "errors": len(errors),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    await asyncio.gather(*(list_dept_users(dept_id) for dept_id in departments))
    return users, errors


def write_outputs(
    departments: list[int],
    users: dict[str, dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    rows = sorted(
        users.values(),
        key=lambda item: (_text(item.get("name")), _text(item.get("job_number")), _text(item.get("userid"))),
    )
    for row in rows:
        row["seen_dept_ids"] = sorted(int(item) for item in row.get("seen_dept_ids") or [])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(
            {
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "department_count": len(departments),
                "unique_user_count": len(rows),
                "error_count": len(errors),
                "errors": errors,
                "users": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    fields = ["userid", "name", "job_number", "title", "mobile", "active", "dept_id_list", "seen_dept_ids"]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["dept_id_list"] = json.dumps(out.get("dept_id_list") or [], ensure_ascii=False)
            out["seen_dept_ids"] = json.dumps(out.get("seen_dept_ids") or [], ensure_ascii=False)
            writer.writerow(out)

    OUT_TXT.write_text(
        "\n".join(_text(row.get("userid")) for row in rows if _text(row.get("userid"))) + "\n",
        encoding="utf-8",
    )


async def main() -> None:
    started = time.time()
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=30.0) as client:
        departments = await collect_departments(client, token)
        print(
            json.dumps({"stage": "departments_done", "department_count": len(departments)}, ensure_ascii=False),
            flush=True,
        )
        users, errors = await collect_users(client, token, departments)
    write_outputs(departments, users, errors)
    print(
        json.dumps(
            {
                "ok": len(errors) == 0,
                "department_count": len(departments),
                "unique_user_count": len(users),
                "error_count": len(errors),
                "json": str(OUT_JSON),
                "csv": str(OUT_CSV),
                "txt": str(OUT_TXT),
                "elapsed_sec": round(time.time() - started, 2),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
