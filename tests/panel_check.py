"""无宿主环境下核对面板 DOM：布局尺寸、卡片不重叠、关键字段确已渲染。

与 panel_preview.py 共用同一份 fixture 与 stub bridge，只是把渲染结果量出来，
输出 JSON 供人/脚本核对（无法直接看图时用它兜住“渲染是否真的正常”）。

用法（宿主机）：
    python3 tests/panel_check.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from panel_preview import build_preview, find_chromium  # noqa: E402

PROBE = """
<script>
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  await wait(700);
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { top: Math.round(r.top), left: Math.round(r.left), width: Math.round(r.width), height: Math.round(r.height) };
  };
  const cards = [...document.querySelectorAll(".card")].map((el) => {
    const title = el.querySelector("h2");
    return { title: title ? title.textContent.trim() : "", ...rect(el) };
  });
  const overlaps = [];
  for (let i = 0; i < cards.length; i += 1) {
    for (let j = i + 1; j < cards.length; j += 1) {
      const a = cards[i];
      const b = cards[j];
      const hit =
        a.left < b.left + b.width &&
        b.left < a.left + a.width &&
        a.top < b.top + b.height &&
        b.top < a.top + a.height;
      if (hit) overlaps.push([a.title, b.title]);
    }
  }
  const values = {};
  document.querySelectorAll("input[id], select[id], textarea[id]").forEach((el) => {
    values[el.id] = el.type === "checkbox" ? el.checked : el.value;
  });
  const clipped = [...document.querySelectorAll(".card *")]
    .filter((el) => el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0)
    .map((el) => el.className || el.tagName);
  const badge = document.getElementById("state-badge");
  const metrics = {
    viewport: { width: window.innerWidth, height: window.innerHeight },
    documentHeight: document.documentElement.scrollHeight,
    horizontalOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    badge: badge ? { text: badge.textContent.trim(), tone: badge.dataset.tone } : null,
    cards,
    overlaps,
    clipped,
    counts: {
      intents: document.querySelectorAll("#intent-list li").length,
      runs: document.querySelectorAll("#run-list li").length,
      sessions: document.querySelectorAll("#f-session option").length,
    },
    values,
    footer: {
      now: document.getElementById("foot-now")?.textContent.trim(),
      tz: document.getElementById("foot-tz")?.textContent.trim(),
    },
    runHeadSample: document.querySelector("#run-list li .run-head")?.textContent.trim(),
  };
  const out = document.createElement("pre");
  out.id = "autonomy-metrics";
  out.textContent = JSON.stringify(metrics);
  document.body.appendChild(out);
})();
</script>
"""


def main() -> int:
    chromium = find_chromium()
    with tempfile.TemporaryDirectory(prefix="autonomy-check-") as tmp:
        page = build_preview(Path(tmp))
        html = page.read_text(encoding="utf-8")
        html = html.replace("</body>", PROBE + "\n  </body>")
        page.write_text(html, encoding="utf-8")
        cmd = [
            chromium,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--window-size=1440,2400",
            "--virtual-time-budget=4000",
            "--dump-dom",
            page.as_uri(),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        dom = result.stdout
    marker_start = dom.find('<pre id="autonomy-metrics">')
    if marker_start < 0:
        print(result.stderr[-2000:])
        print("没有拿到 metrics：页面脚本可能报错了")
        return 1
    payload = dom[marker_start:].split(">", 1)[1].split("</pre>", 1)[0]
    payload = (
        payload.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    )
    try:
        metrics = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(payload[:2000])
        raise SystemExit(f"metrics 不是合法 JSON：{exc}") from exc
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
