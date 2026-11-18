"""在没有宿主的环境里渲染自主性面板，产出可核对的截图。

做法：把 views/autonomy 的 index.html / style.css / app.js 复制到临时目录，
在 app.js 之前插入一个 stub bridge（返回固定快照，不联网），然后用系统
Chromium 的无头模式截图。临时目录用完即删，只有截图进仓库。

用法（宿主机）：
    python3 tests/panel_preview.py [输出路径]
默认输出 reports/autonomy-panel-preview.png。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEW = ROOT / "views" / "autonomy"
DEFAULT_OUT = ROOT / "reports" / "autonomy-panel-preview.png"
CHROMIUM_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
)

FIXTURE = {
    "plugin": "astrbot_plugin_autonomy",
    "now": "2026-11-18T14:41:03+08:00",
    "timezone": "Asia/Shanghai",
    "settings": {
        "enabled": True,
        "mode": "adaptive",
        "interval_minutes": 30,
        "probability": 0.2,
        "recent_minutes": 20,
        "recent_multiplier": 0.15,
        "distant_minutes": 360,
        "distant_multiplier": 1.5,
        "sleep_windows": "23:30-08:30",
        "timezone": "Asia/Shanghai",
        "motivation": "醒着就看看主人在不在，没有什么事就安静待着。",
        "owner_id": "1250226509",
        "allow_self_wake": True,
        "self_wake_during_sleep": False,
        "sessions": [
            "astrbot-mc:FriendMessage:1250226509",
            "astrbot-mc:GroupMessage:685164226",
            "astrbot-mc:GroupMessage:1034185861",
        ],
    },
    "sessions": [
        "astrbot-mc:FriendMessage:1250226509",
        "astrbot-mc:GroupMessage:685164226",
        "astrbot-mc:GroupMessage:1034185861",
    ],
    "current": "astrbot-mc:GroupMessage:685164226",
    "sleeping": False,
    "sleep_windows": [{"start": "23:30", "end": "08:30"}],
    "probability": 0.2,
    "last_activity_at": "2026-11-18T14:20:41+08:00",
    "next_check_at": "2026-11-18T14:52:48+08:00",
    "self_wake": {
        "at": "2026-11-18T21:55:00+08:00",
        "note": "主人说九点五十五分要忙完，到点再问一句",
        "version": 2,
        "trigger": "self",
    },
    "intents": [
        {
            "id": "it-1",
            "kind": "agreement",
            "state": "open",
            "text": "主人说九点五十五忙完，到时候问他要不要一起看看插件面板。",
            "due_at": "2026-11-18T21:55:00+08:00",
            "version": 2,
        },
        {
            "id": "it-2",
            "kind": "thought",
            "state": "open",
            "text": "想确认一下群里那位新成员是不是还在等回复。",
            "preferred_at": "2026-11-18T22:30:00+08:00",
            "version": 1,
        },
    ],
    "runs": [
        {
            "run_id": "run-a",
            "trigger": "random",
            "status": "completed",
            "decision": "no_action",
            "reason": "主人二十五分钟前刚说还在修 bug，这会儿不该插话。本轮不发消息、不动别的，安静待着；等主人自己忙完再陪聊。",
            "started_at": "2026-11-18T14:30:48+08:00",
            "ended_at": "2026-11-18T14:31:26+08:00",
        },
        {
            "run_id": "run-b",
            "trigger": "self",
            "status": "completed",
            "decision": "acted",
            "reason": "昨晚约好的时间到了，按约定开口问了一句面板的事。",
            "started_at": "2026-11-17T21:55:02+08:00",
            "ended_at": "2026-11-17T21:56:10+08:00",
        },
        {
            "run_id": "run-c",
            "trigger": "manual",
            "status": "cancelled",
            "decision": None,
            "reason": "由面板叫停：主人正在用这个会话，先把排队的判断撤了。",
            "started_at": "2026-11-17T20:10:00+08:00",
            "ended_at": "2026-11-17T20:10:04+08:00",
        },
    ],
    "queued": 0,
    "running": False,
}

STUB = """
(function () {
  const fixture = __FIXTURE__;
  const bridge = {
    ready: () => Promise.resolve(true),
    getContext: () => ({}),
    getLocale: () => "zh-CN",
    getI18n: () => ({}),
    t: (_key, fallback) => fallback || "",
    onContext: () => {},
    apiGet: () => Promise.resolve({ success: true, data: fixture }),
    apiPost: () => Promise.resolve({ success: true, data: {} }),
    upload: () => Promise.resolve({ success: true, data: {} }),
    download: () => Promise.resolve(null),
    subscribeSSE: () => "stub",
    unsubscribeSSE: () => {},
  };
  window.AstrBotPluginView = bridge;
  window.AstrBotPluginPage = bridge;
})();
"""


def find_chromium() -> str:
    override = os.environ.get("CHROMIUM_BIN")
    if override:
        return override
    for candidate in CHROMIUM_CANDIDATES:
        found = shutil.which(candidate) if not candidate.startswith("/") else (
            candidate if Path(candidate).exists() else None
        )
        if found:
            return found
    raise SystemExit("没有找到 Chromium，可通过 CHROMIUM_BIN 指定")


def build_preview(target: Path) -> Path:
    html = (VIEW / "index.html").read_text(encoding="utf-8")
    stub = STUB.replace("__FIXTURE__", json.dumps(FIXTURE, ensure_ascii=False))
    html = html.replace(
        '<script src="app.js"></script>',
        f"<script>{stub}</script>\n    <script src=\"app.js\"></script>",
    )
    (target / "style.css").write_text((VIEW / "style.css").read_text(encoding="utf-8"), encoding="utf-8")
    # 截图专用：关掉过渡动画，避免截到开关颜色过渡的中间帧
    html = html.replace(
        "</head>",
        "<style>*{transition:none!important;animation:none!important}</style>\n  </head>",
        1,
    )
    (target / "app.js").write_text((VIEW / "app.js").read_text(encoding="utf-8"), encoding="utf-8")
    page = target / "index.html"
    page.write_text(html, encoding="utf-8")
    return page


def main() -> int:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    chromium = find_chromium()
    with tempfile.TemporaryDirectory(prefix="autonomy-panel-") as tmp:
        page = build_preview(Path(tmp))
        cmd = [
            chromium,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--window-size=1440,2400",
            "--virtual-time-budget=3000",
            f"--screenshot={out}",
            page.as_uri(),
        ]
        if out.exists():
            out.unlink()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        # Chrome 可能以 0 退出却什么也没写（GUI 子系统静默失败），所以只认文件本身
        if not out.exists() or out.stat().st_size == 0:
            print(result.stdout[-2000:])
            print(result.stderr[-2000:])
            raise SystemExit(f"截图没有生成：{out}")
        print(f"截图已生成：{out}（{out.stat().st_size} 字节）")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
