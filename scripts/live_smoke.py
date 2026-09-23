#!/usr/bin/env python
"""真实 API 冒烟测试 — 仅使用当前免费的 flash 模型。

覆盖: 文本 chat、文生图 (1K)、文生视频 (4s 720P, 异步轮询)。
用法:
    source ~/.zshenv   # 提供 AGNES_API_KEY
    python scripts/live_smoke.py [--skip-video] [--only-video]

绝不打印 API Key。视频轮询复用插件 service 的 URL/payload 构建函数，
确保冒烟验证的就是插件实际发送的请求形态。
"""

import asyncio
import importlib.util
import json
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("agnes_ai_generation", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
_mod = importlib.util.module_from_spec(_spec)
sys.modules["agnes_ai_generation"] = _mod
_spec.loader.exec_module(_mod)

from agnes_ai_generation import service  # noqa: E402

TEXT_MODEL = "agnes-2.5-flash"
IMAGE_MODEL = "agnes-image-2.5-flash"
VIDEO_MODEL = "agnes-video-2.5-flash"


async def check_text(client):
    # max_tokens 要给足：flash 模型带 reasoning，太小时内容全被 reasoning 吃掉
    data = await service._req(client, "POST", "/v1/chat/completions", {
        "model": TEXT_MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: AGNES-TEXT-OK"}],
        "max_tokens": 2000, "temperature": 0,
    })
    content = data["choices"][0]["message"]["content"]
    print(f"[text]  {TEXT_MODEL}: {content!r}")
    assert "AGNES-TEXT-OK" in content, content


async def check_image(client):
    data = await service._req(client, "POST", "/v1/images/generations", {
        "model": IMAGE_MODEL,
        "prompt": "A small red cube on a white table, studio lighting",
        "size": "1K", "ratio": "1:1",
        "extra_body": {"response_format": "url"},
    })
    urls = service.extract_image_urls(data)
    print(f"[image] {IMAGE_MODEL}: {len(urls)} url(s), first={urls[0][:80] if urls else None}")
    assert urls, data


async def check_video(client):
    payload = service.build_video_payload(
        "A cat walking on the beach at sunset, cinematic",
        VIDEO_MODEL, "text", "4", "720P", "16:9",
    )
    created = None
    for attempt in range(1, 6):
        try:
            created = await service._req(client, "POST", "/v1/videos", payload)
            break
        except RuntimeError as exc:
            if "503" not in str(exc) or attempt == 5:
                raise
            print(f"[video] create attempt {attempt} rejected (queue full), retry in 30s")
            await asyncio.sleep(30)
    video_id = created.get("video_id") or created.get("videoId") or created.get("id")
    print(f"[video] {VIDEO_MODEL} created: video_id={video_id} status={created.get('status')}")
    assert video_id, created

    path = service._poll_path(video_id, VIDEO_MODEL)
    deadline = time.time() + 600
    # 实测 2.5 队列态会经过 queued / pending / in_progress，只有 completed|failed 是终态
    non_terminal = ("queued", "pending", "in_progress", "processing")
    n = 0
    data = {}
    st = ""
    while time.time() < deadline:
        await asyncio.sleep(2)
        n += 1
        data = await service._req(client, "GET", path)
        st = str(data.get("status", "")).lower()
        if st not in non_terminal:
            print(f"[video] poll#{n} status={st}")
            break
        if n % 15 == 0:
            print(f"[video] poll#{n} status={st} progress={data.get('progress')}")
    urls = service.extract_video_urls(data)
    print(f"[video] final={st} urls={urls}")
    assert st == "completed" and urls, json.dumps(data, ensure_ascii=False)[:300]


async def main():
    skip_video = "--skip-video" in sys.argv
    video_only = "--only-video" in sys.argv
    async with httpx.AsyncClient() as client:
        if not video_only:
            await check_text(client)
            await check_image(client)
        if skip_video:
            print("[video] skipped (--skip-video)")
        else:
            await check_video(client)
    print("LIVE SMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
