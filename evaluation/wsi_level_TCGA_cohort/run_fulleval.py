#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
无头(headless)批量驱动 —— UCEC / LUAD / COAD 三癌种 TCGA 队列评测。

设计要点（后端分析逻辑与 Web 应用相同）：
- 直接调用核心 agent 函数 run_agent(mode='ai')；切片路径写入 file_registry.uploaded_files；
- 唯一外部差异：用一个 MockSocketIO 替代真实 socketio（emit 空操作、sleep=time.sleep）；
  其余切图 / 多尺度下钻 / R-B-G 框选 / 癌种问题报告生成 全部走原代码路径，不改任何分析逻辑。
- 每张切片跑完后：
    1) 原代码已把结果落盘到 outputs/{文件名}_{时间戳}/auto/
       （pathology_report.txt = 癌种问题回答，analysis_tree.json = R/B/G 框 level0 坐标，
        steps/step_0_thumbnail.jpg = 缩略底图）；
    2) 本脚本额外在缩略图上复刻一张"带框选的前端样式可视化图"(10×红/40×蓝/160×绿)；
    3) 汇总进一张总表 results_index_<cancer>.csv（slide, patient, output_dir, viz, 报告原文, error）
       供后续由病理医生对照参考答案人工评分。
- 断点续跑：已出 pathology_report.txt 的切片自动跳过。
- 单张失败不影响整批（try/except 包裹，记录 error 继续）。

用法（需在已安装 openslide 的 Python 环境中运行）：
  export OPENROUTER_API_KEY="<your-openrouter-key>"   # 仅通过环境变量提供
  python run_fulleval.py --cancer ucec --svs_dir ./TCGA-UCEC-svs --lang zh
  python run_fulleval.py --cancer luad --svs_dir ./TCGA-LUAD-svs --lang zh
  python run_fulleval.py --cancer coad --svs_dir ./TCGA-COAD-svs --lang zh
  python run_fulleval.py --svs_dir ./TCGA-UCEC-svs --slide_list mylist.txt --limit 2   # 先验证少量
"""
import os, sys, csv, glob, json, time, uuid, argparse, traceback, logging
from datetime import datetime

# ---- 扁平化目录：runner、Agent 与注册表均位于当前评测根目录 ----
HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY_DIR = HERE
COHORT_ROOT = HERE
COMMON_DIR = os.path.join(COHORT_ROOT, "common")
MCP_CONFIG = os.path.join(COHORT_ROOT, "mcp.outbound.json")


class MockSocketIO:
    """替代真实 socketio：mode='ai' 链路只用到 emit / sleep。"""
    def emit(self, *args, **kwargs):
        return None

    def sleep(self, seconds=0):
        time.sleep(seconds or 0)


# ROI 层级配色（与前端 static/pathchat.css 一致：10×红 / 40×蓝 / 160×绿）
LEVEL_COLOR = {
    "red": (255, 0, 0), "blue": (0, 102, 255), "green": (0, 204, 0),
    10: (255, 0, 0), 40: (0, 102, 255), 160: (0, 204, 0),
}


def _zoom_to_color(node):
    lvl = (node.get("level") or "").lower()
    if lvl in LEVEL_COLOR:
        return LEVEL_COLOR[lvl], lvl
    z = node.get("zoom_level") or node.get("zoom") or ""
    z = str(z).replace("×", "").replace("x", "").strip()
    try:
        zi = int(float(z))
        return LEVEL_COLOR.get(zi, (255, 255, 0)), f"{zi}x"
    except Exception:
        return (255, 255, 0), lvl or "?"


def _iter_nodes(nodes):
    """深度遍历 tree_nodes，产出 (node, rect)。rect 可能在顶层或 analysis 里。"""
    for n in nodes or []:
        rect = n.get("rect") or (n.get("analysis", {}) or {}).get("rect")
        if rect and all(k in rect for k in ("x", "y", "w", "h")):
            yield n, rect
        yield from _iter_nodes(n.get("children"))


def _level_rank(node):
    """层级排序：红(0)→蓝(1)→绿(2)，确保大框先画、小框后画(在上层、不被遮挡)。"""
    lvl = (node.get("level") or "").lower()
    if "red" in lvl: return 0
    if "blue" in lvl: return 1
    if "green" in lvl: return 2
    z = str(node.get("zoom_level") or node.get("zoom") or "").replace("×", "").replace("x", "").strip()
    try:
        zi = int(float(z)); return {10: 0, 40: 1, 160: 2}.get(zi, 1)
    except Exception:
        return 1


def render_roi_viz(output_dir, svs_path, save_path, long_side=4000):
    """从 analysis_tree.json 的 level0 坐标，在高清缩略图上复刻带框可视化图(接近前端高清截图)。
    - 底图按 long_side(默认4000px)从 openslide 重取，更清晰；
    - 框线按图尺寸与层级加粗(红粗→绿细)，标签用 TrueType 大字体；
    - 大框先画、小框后画，避免绿框被红/蓝覆盖。"""
    from PIL import Image, ImageDraw
    import openslide
    from agent.font_utils import load_grid_label_font

    tree_json = os.path.join(output_dir, "analysis_tree.json")
    if not os.path.exists(tree_json):
        return None, "无 analysis_tree.json"
    with open(tree_json, encoding="utf-8") as f:
        tree = json.load(f)
    nodes = tree.get("tree_nodes", [])

    # 高清底图：直接从 svs 取大尺寸缩略图(比 steps 里的 2000px 清晰)
    sl = openslide.OpenSlide(svs_path)
    W0, H0 = sl.dimensions
    img = sl.get_thumbnail((long_side, long_side)).convert("RGB")
    sl.close()
    sx, sy = img.width / W0, img.height / H0

    draw = ImageDraw.Draw(img)
    base_w = max(2, img.width // 1100)                 # 随图尺寸缩放的基准线宽
    lw = {0: base_w + 3, 1: base_w + 1, 2: base_w}     # 红粗/蓝中/绿细
    fsz = max(12, img.width // 120)
    font, font_source = load_grid_label_font(fsz)
    logging.getLogger(__name__).info(
        "ROI viz font requested_px=%s source=%s", fsz, font_source
    )

    # 收集所有框并按层级排序(红→蓝→绿)，保证小框画在最上层
    items = sorted(_iter_nodes(nodes), key=lambda nr: _level_rank(nr[0]))
    n_box = 0
    for node, rect in items:
        color, _ = _zoom_to_color(node)
        rank = _level_rank(node)
        x, y = int(rect["x"] * sx), int(rect["y"] * sy)
        w, h = int(rect["w"] * sx), int(rect["h"] * sy)
        draw.rectangle([x, y, x + w, y + h], outline=color, width=lw[rank])
        rid = node.get("roi_id") or node.get("id") or ""
        if rid and rank <= 1:   # 只给红/蓝标签(绿框太多, 标了反而乱)
            ty = max(0, y - fsz - 2)
            draw.text((x + 3, ty), str(rid), fill=color, font=font)
        n_box += 1

    img.save(save_path, "JPEG", quality=92)
    return n_box, ""


def _candidate_dirs(svs_basename):
    """该样本可能的输出 auto 目录列表。
    - 设了 TAG: 固定 outputs/{TAG}/{safe}/auto
    - 未设 TAG: 兼容原结构 outputs/{safe}_*/auto"""
    from agent.agent_loop import sanitize_filename
    safe = sanitize_filename(svs_basename)
    tag = os.environ.get("PATHCOPILOT_OUTPUT_TAG", "").strip()
    if tag:
        return [os.path.join("outputs", tag, safe, "auto")]
    return [d for d in glob.glob(os.path.join("outputs", f"{safe}_*", "auto")) if os.path.isdir(d)]


def find_output_dir(svs_basename, before_ts):
    """run_agent 跑完后定位其输出目录。"""
    cands = [d for d in _candidate_dirs(svs_basename) if os.path.isdir(d)]
    return max(cands, key=os.path.getmtime) if cands else None


def _report_is_empty(txt):
    """最终报告正文为空，或综合调用失败。"""
    parts = txt.split("=" * 60)
    body = parts[1].strip() if len(parts) > 1 else txt.strip()
    return not body or '{"selected_grids"' in txt or len(txt.strip()) < 200


def already_done(svs_basename):
    """断点续跑：已有"有效"pathology_report.txt 才算完成。
    空壳报告({"selected_grids": []} 或过短)不算完成，会重新运行。"""
    for d in _candidate_dirs(svs_basename):
        rpt = os.path.join(d, "pathology_report.txt")
        if os.path.exists(rpt):
            try:
                txt = open(rpt, encoding="utf-8").read()
            except Exception:
                continue
            if _report_is_empty(txt):
                continue  # 空壳, 不算完成
            return d
    return None


def _attach_sample_log(basename):
    """给当前样本挂一个 root logger 文件处理器，把完整终端日志写进该样本输出目录，
    便于后期逐样本溯源。返回 (handler, log_path) 或 (None, None)。"""
    from agent.agent_loop import sanitize_filename
    tag = os.environ.get("PATHCOPILOT_OUTPUT_TAG", "").strip()
    if not tag:
        return None, None
    auto_dir = os.path.join("outputs", tag, sanitize_filename(basename), "auto")
    os.makedirs(auto_dir, exist_ok=True)
    log_path = os.path.join(auto_dir, "terminal_log.txt")
    h = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                                     datefmt="%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    if root.level > logging.INFO or root.level == 0:
        root.setLevel(logging.INFO)
    root.addHandler(h)
    return h, log_path


def run_one(svs_path, lang, max_steps, do_viz, runtime=None):
    from file_registry import uploaded_files
    from agent.agent_loop import run_agent

    basename = os.path.basename(svs_path)
    patient = "-".join(basename.split("-")[:3])

    # 注册 file_id → 路径（run_agent 通过 uploaded_files / get_wsi_file_path 解析）
    file_id = str(uuid.uuid4())
    uploaded_files[file_id] = {
        "file_id": file_id,
        "filename": basename,
        "file_path": os.path.abspath(svs_path),
    }

    # ⭐ 挂样本级终端日志(写进该样本输出目录 terminal_log.txt)，便于逐样本溯源
    _log_handler, _log_path = _attach_sample_log(basename)
    t0 = time.time()
    runtime_receipt = {
        "mode": "off", "enabled": False, "run_id": "",
        "run_state": "", "error": "",
    }
    try:
        room = f"batch-{file_id}"
        runner_kwargs = dict(
            file_id=file_id,
            max_steps=max_steps,
            strategy="default",
            mode="ai",
            lang=lang,
        )
        if runtime is None:
            run_agent(MockSocketIO(), room=room, **runner_kwargs)
        else:
            _, runtime_receipt = runtime.run(
                run_agent,
                socketio=MockSocketIO(),
                room=room,
                file_id=file_id,
                runner_kwargs=runner_kwargs,
            )
    finally:
        if _log_handler is not None:
            logging.getLogger().removeHandler(_log_handler)
            _log_handler.close()

    out_dir = find_output_dir(basename, t0)
    rec = {"slide": basename, "patient": patient, "output_dir": out_dir or "",
           "viz": "", "n_box": "", "report": "", "error": "",
           "runtime_mode": runtime_receipt.get("mode", ""),
           "runtime_run_id": runtime_receipt.get("run_id", ""),
           "runtime_state": runtime_receipt.get("run_state", ""),
           "runtime_error": runtime_receipt.get("error", "")}
    if not out_dir:
        rec["error"] = "未找到输出目录"
        return rec

    rpt = os.path.join(out_dir, "pathology_report.txt")
    # 只接受本次运行写出的报告；留 2 秒余量兼容低精度文件时间。
    if os.path.exists(rpt) and os.path.getmtime(rpt) >= t0 - 2:
        with open(rpt, encoding="utf-8") as f:
            rec["report"] = f.read()
        # 报告空壳检测：最终综合调用失败时会兜底写入 {"selected_grids": []}，需标记为失败而非当成有效结果
        if _report_is_empty(rec["report"]):
            rec["error"] = "最终报告为空壳(综合调用失败, 疑似上下文超窗)"
    else:
        rec["error"] = "本次运行未生成 pathology_report.txt"

    if do_viz:
        try:
            viz_path = os.path.join(out_dir, "roi_overlay_viz.jpg")
            n_box, verr = render_roi_viz(out_dir, svs_path, viz_path)
            rec["viz"] = viz_path if n_box else ""
            rec["n_box"] = n_box if n_box is not None else ""
            if verr and not n_box:
                rec["error"] = (rec["error"] + " | " + verr).strip(" |")
        except Exception as e:
            rec["error"] = (rec["error"] + f" | viz失败:{e}").strip(" |")
    return rec


def append_index(out_csv, rec):
    cols = [
        "slide", "patient", "output_dir", "viz", "n_box", "error",
        "runtime_mode", "runtime_run_id", "runtime_state", "runtime_error",
        "report",
    ]
    new = not os.path.exists(out_csv)
    with open(out_csv, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({c: rec.get(c, "") for c in cols})


def main():
    ap = argparse.ArgumentParser(description="无头批量驱动 PathCopilot（换基座全量评测）")
    ap.add_argument(
        "--cancer", default="ucec", choices=["ucec", "luad", "coad"],
        help="癌种提示词配置；必须与待测队列一致",
    )
    ap.add_argument("--svs_dir", required=True, help="切片目录(如 ./TCGA-UCEC-svs)")
    ap.add_argument("--slide_list", default=None, help="可选: 只跑清单内的切片(文件名或患者条码)")
    ap.add_argument(
        "--out", default=None,
        help="汇总表路径（默认 results_index_{cancer}.csv 于本脚本目录）",
    )
    ap.add_argument("--lang", default="zh", choices=["zh", "en"])
    ap.add_argument("--provider", default="openrouter", choices=["openrouter"],
                    help="API 提供商（开源版本仅支持 OpenRouter）")
    ap.add_argument("--tag", default=None,
                    help="输出子目录标签；默认按 --cancer 生成。"
                         "结果落在 outputs/{tag}/{样本名}/auto/")
    ap.add_argument("--max_steps", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help=">0 时只跑前 N 张(用于验证)")
    ap.add_argument("--no_viz", action="store_true", help="不生成带框可视化图")
    ap.add_argument("--no_resume", action="store_true", help="不跳过已完成的切片")
    ap.add_argument(
        "--runtime-mode", default="shadow", choices=["off", "shadow"],
        help="Agent Runtime 模式；默认 shadow，与开源应用默认值一致",
    )
    ap.add_argument(
        "--runtime-db", default=None,
        help="Agent Runtime SQLite 路径",
    )
    ap.add_argument(
        "--outbound-mcp", action="store_true",
        help="开启 PubMed/Web Search 出站 MCP（默认关闭，与正式评测一致）",
    )
    ap.add_argument(
        "--no-outbound-mcp", action="store_true",
        help="保持出站 MCP 关闭（默认即关闭；保留以兼容旧命令）",
    )
    args = ap.parse_args()
    question_count = {"ucec": 6, "luad": 7, "coad": 7}[args.cancer]

    # ⭐ 必须在 import gpt_client/agent 之前设好 provider(gpt_client 在 import 时读取它)
    os.environ["API_PROVIDER"] = "openrouter"
    os.environ["PATHCOPILOT_CANCER_PROFILE"] = args.cancer
    outbound_mcp_enabled = args.outbound_mcp and not args.no_outbound_mcp
    os.environ["PATHCOPILOT_OUTBOUND_MCP"] = "1" if outbound_mcp_enabled else "0"
    os.environ["PATHCOPILOT_OUTBOUND_MCP_CONFIG"] = MCP_CONFIG
    os.environ["PATHCOPILOT_AGENT_DIR"] = os.path.join(DEPLOY_DIR, "agent")

    svs_dir = os.path.abspath(args.svs_dir)
    out_csv = (
        os.path.abspath(args.out) if args.out
        else os.path.join(HERE, f"results_index_{args.cancer}.csv")
    )
    runtime_db = os.path.abspath(
        args.runtime_db
        or os.path.join(HERE, "runtime_data", f"runtime_{args.cancer}.sqlite3")
    )
    slide_keys = None
    if args.slide_list and os.path.exists(args.slide_list):
        slide_keys = {l.strip() for l in open(os.path.abspath(args.slide_list)) if l.strip()}

    svs = sorted(glob.glob(os.path.join(svs_dir, "*.svs")))
    if slide_keys is not None:
        svs = [p for p in svs if os.path.basename(p) in slide_keys
               or "-".join(os.path.basename(p).split("-")[:3]) in slide_keys]
    if args.limit > 0:
        svs = svs[:args.limit]
    if not svs:
        print(f"[ERROR] {svs_dir} 下没有匹配的 svs"); sys.exit(1)

    # ⭐ run_agent 用相对路径写 outputs/、logs/、uploads/，必须把 cwd 切到部署目录
    os.chdir(DEPLOY_DIR)
    sys.path.insert(0, COMMON_DIR)
    sys.path.insert(0, DEPLOY_DIR)

    from agent.gpt_client import DEFAULT_MODEL
    from cohort_runtime import CohortAgentRuntime

    runtime = CohortAgentRuntime(
        runtime_db,
        mode=args.runtime_mode,
    )
    runtime.start()

    tag = args.tag or f"TCGA-{args.cancer.upper()}-PathCopilot"
    os.environ["PATHCOPILOT_OUTPUT_TAG"] = tag   # get_output_paths 读取它决定输出结构

    print("=" * 80)
    print(f"无头批量评测 | 基座模型(DEFAULT_MODEL): {DEFAULT_MODEL}")
    print(f"癌种提示词: {args.cancer.upper()} ({question_count} 问)")
    print(f"输出标签(TAG): {tag}  →  outputs/{tag}/{{样本名}}/auto/")
    print(f"切片数: {len(svs)}  语言: {args.lang}  max_steps: {args.max_steps}")
    print(f"部署代码: {DEPLOY_DIR}")
    print(f"Agent Runtime: {args.runtime_mode}  DB: {runtime_db}")
    print(f"Outbound MCP: {'开启（PubMed + Web Search）' if outbound_mcp_enabled else '关闭'}")
    print(f"汇总表: {out_csv}")
    print("=" * 80)

    done = ok = fail = skip = 0
    try:
        for i, p in enumerate(svs, 1):
            base = os.path.basename(p)
            if not args.no_resume:
                d = already_done(base)
                if d:
                    skip += 1
                    print(f"[{i}/{len(svs)}] ⏭️  跳过(已完成) {base[:46]}")
                    continue
            print(f"[{i}/{len(svs)}] ▶️  开始 {base[:46]}")
            t0 = time.time()
            try:
                rec = run_one(
                    p, args.lang, args.max_steps, not args.no_viz,
                    runtime=runtime,
                )
            except Exception as e:
                rec = {"slide": base, "patient": "-".join(base.split("-")[:3]),
                       "output_dir": "", "viz": "", "n_box": "", "report": "",
                       "runtime_mode": args.runtime_mode, "runtime_run_id": "",
                       "runtime_state": "failed", "runtime_error": str(e),
                       "error": f"{type(e).__name__}: {e}"}
                traceback.print_exc()
            append_index(out_csv, rec)
            done += 1
            if rec["error"]:
                fail += 1
                print(f"           ✗ {rec['error'][:80]}  ({time.time()-t0:.0f}s)")
            else:
                ok += 1
                print(f"           ✓ 框{rec.get('n_box','?')}个 → {rec['output_dir']}  ({time.time()-t0:.0f}s)")
    finally:
        runtime.stop()

    print("=" * 80)
    print(f"完成: 成功{ok} 失败{fail} 跳过{skip} (本轮处理{done}) → {out_csv}")
    print(f"后续：由病理医生对照 {question_count} 个问题的参考答案，对 report 原文人工评分。")


if __name__ == "__main__":
    main()
