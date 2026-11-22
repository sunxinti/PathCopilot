#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, csv, json, ast, time, uuid, glob, argparse, re, traceback
import hashlib
import importlib
import tempfile
from collections import defaultdict, OrderedDict

# Benchmark hard boundary: the product may support outbound MCP, but this
# evaluator must never activate PubMed/Web Search MCP transports.  Keep this
# assignment before any PathCopilot Agent import.
os.environ["PATHCOPILOT_OUTBOUND_MCP"] = "0"
# Do not create or reuse local bytecode in the benchmark process.  This does
# not replace the module-origin validation below, but prevents stale .pyc files
# from obscuring which snapshot is executing.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

csv.field_size_limit(10 ** 7)

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY_DIR = HERE
DEF_CSV = os.path.join(HERE, "MultiPathQA.csv")
DEF_IMG_DIR = os.path.join(HERE, "images", "tcga")

_LETTERS = ["A", "B", "C", "D", "E", "F"]
# Legacy routing IDs (10/40/160), not optical magnifications. See agent_loop.py.
VIEW_PROFILE = "thumbnail/10x/40x/160x"
MASK_POLICY = "relaxed_gray_std_no_morphology_v3_tissue15_fail_closed"
TISSUE_THRESHOLD = 0.15
PROTOCOL_REVISION = "multipathqa_primary_candidate_filter_v3_fail_closed_knowledge_scope_v1"


def _is_within(path, root):
    path = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def activate_local_deployment():
    """Import the benchmark's registry/Agent modules only from this snapshot.

    A long-lived Python process can retain another PathCopilot checkout in
    ``sys.modules``.  Clear those names, invalidate import caches, put this
    directory first, then fail before any paid call if a module resolves
    outside the benchmark snapshot.
    """
    for name in list(sys.modules):
        if name in {"app", "agent"} or name.startswith("agent."):
            sys.modules.pop(name, None)

    deploy_real = os.path.realpath(DEPLOY_DIR)
    sys.path[:] = [
        p for p in sys.path
        if os.path.normcase(os.path.realpath(p or os.curdir))
        != os.path.normcase(deploy_real)
    ]
    sys.path.insert(0, DEPLOY_DIR)
    importlib.invalidate_caches()
    os.chdir(DEPLOY_DIR)

    modules = [
        importlib.import_module("app"),
        importlib.import_module("agent.agent_loop"),
        importlib.import_module("agent.gpt_client"),
    ]
    for module in modules:
        module_path = getattr(module, "__file__", "")
        if not module_path or not _is_within(module_path, DEPLOY_DIR):
            raise RuntimeError(
                "Local Python module cache/path contamination: "
                f"{module.__name__} resolved to {module_path!r}, "
                f"expected a file under {DEPLOY_DIR!r}"
            )
    return modules


def source_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluation_fingerprint(row, idx, run_context):
    """Bind a score to its inputs, WSI file metadata and evaluation configuration."""
    image_path = os.path.normcase(os.path.abspath(row["svs"]))
    image_stat = os.stat(image_path)
    payload = {
        "benchmark": row["benchmark"], "idx": str(idx),
        "question": row["prompt"], "options": row["options"],
        "gold": row["gold"], "metric_type": row["metric_type"],
        "file_id": row.get("file_id", ""), "image_path": image_path,
        "image_size": image_stat.st_size, "image_mtime_ns": image_stat.st_mtime_ns,
        "run_context": run_context,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def resume_record_is_compatible(rec, agent_loop_sha256, expected_fingerprint):
    """Only reuse results with matching ROI protocol, inputs and configuration."""
    if not isinstance(rec, dict) or not expected_fingerprint:
        return False
    return (
        rec.get("view_profile") == VIEW_PROFILE
        and rec.get("tissue_mask_policy") == MASK_POLICY
        and rec.get("tissue_content_threshold") == TISSUE_THRESHOLD
        and rec.get("protocol_revision") == PROTOCOL_REVISION
        and rec.get("agent_loop_sha256") == agent_loop_sha256
        and rec.get("evaluation_fingerprint") == expected_fingerprint
    )


class MockSocketIO:
    def emit(self, *a, **k): return None
    def sleep(self, s=0): time.sleep(s or 0)


# ───────────────────────── 题库加载 ─────────────────────────
def load_rows(csv_path, benchmark):
    """读 CSV，过滤指定子 benchmark，解析 options/gold。返回 list[dict]。"""
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["benchmark_name"] != benchmark:
                continue
            try:
                opts = ast.literal_eval(r["options"]) if r.get("options") else []
            except Exception:
                opts = []
            opts = [str(o) for o in opts]
            gold = ""
            ans = (r.get("answer") or "").strip()
            if opts and ans.isdigit():
                # tcga_expert_vqa / tcga / tcga_slidebench: answer = 1-based 下标
                gi = int(ans) - 1  # ⚠️ 1-based 下标
                if 0 <= gi < len(opts):
                    gold = opts[gi]
            elif opts:
                # GTEx: answer 直接是选项文本(器官名)，归一到选项原文以便与抽取结果对分
                gold = next((o for o in opts if _norm(o) == _norm(ans)), ans)
            else:
                # PANDA: 无选项，answer 即标准答案(ISUP 等级 0–5)
                gold = ans
            rows.append({
                "benchmark": r["benchmark_name"],
                "bid": r.get("benchmark_id", ""),
                "file_id": (r.get("file_id") or "").strip(),
                "image_path": r["image_path"],
                "prompt": (r.get("prompt") or "").strip(),
                "options": opts,
                "answer_idx": ans,
                "gold": gold,
                "metric_type": r.get("metric_type", "accuracy"),
            })
    return rows


_WSI_EXT = (".svs", ".tiff", ".tif", ".ndpi", ".scn")


def resolve_image(img_dir, image_path, file_id=None):
    """解析切片绝对路径，按以下顺序找(命名/存放方式因下载方式而异)：
      1) 散装文件: img_dir/<basename(image_path)>             (条形码命名的 SVS 文件)
      2) gdc-client UUID 目录: img_dir/<file_id>/*.svs        (slidebench/部分 tcga)
      3) file_id 直接命名: img_dir/<file_id>[.ext]            (panda 全名带扩展 / gtex 无扩展)
    """
    p = os.path.join(img_dir, os.path.basename(image_path))
    if os.path.exists(p):
        return os.path.abspath(p)
    if file_id:
        d = os.path.join(img_dir, file_id)
        if os.path.isdir(d):
            cands = [f for f in os.listdir(d) if f.lower().endswith(_WSI_EXT)]
            if cands:
                return os.path.abspath(os.path.join(d, cands[0]))
        for cand in (file_id, *(file_id + e for e in _WSI_EXT)):
            pp = os.path.join(img_dir, cand)
            if os.path.exists(pp):
                return os.path.abspath(pp)
    return None


# ───────────────────────── 问题格式化(器官中立) ─────────────────────────
def format_question_block(prompt, options, lang="en"):
    """把单题(题干+选项)格式化为器官中立的'诊断目标'块，注入 CLINICAL_QUESTIONS。

    按官方模板约定处理 prompt：
      - 反转义 Python format 的 {{ }} → { }（PANDA/TCGA/GTEx 题面含此转义）
      - 若题面含 {options} 占位符(TCGA/GTEx)，就地填入 1-based 编号选项；
        ExpertVQA/SlideBench 无占位符则由本函数另起一段列出选项。
      - PANDA 无选项：保留题面自带的 ISUP JSON 作答指令，不强加多选包装。
    """
    prompt = (prompt or "").replace("{{", "{").replace("}}", "}")
    numbered = "\n".join(f"{j + 1}. {c}" for j, c in enumerate(options)) if options else ""
    has_placeholder = "{options}" in prompt
    if has_placeholder:
        prompt = prompt.replace("{options}", numbered)

    lines = []
    if lang == "en":
        lines.append("You are analyzing ONE whole-slide histopathology (H&E) image. "
                     "Your objective is to answer the following question about THIS slide by collecting "
                     "visual evidence across magnifications (thumbnail → low → high power) and across "
                     "different spatial regions of the slide.\n")
        if options and not has_placeholder:
            lines.append(f"Question: {prompt}\n\nOptions:\n{numbered}")
        else:
            lines.append(f"Question: {prompt}")
        if options:
            lines.append("(Multiple choice — reason from what you actually SEE in the image FIRST "
                         "(morphology, spatial location, counts, etc.), then choose EXACTLY ONE of the "
                         "options above and state it VERBATIM. If evidence is insufficient, still pick "
                         "the single MOST LIKELY option — never refuse or leave it blank.)")
        else:
            lines.append("(Reason from what you actually SEE in the image FIRST (morphology, "
                         "architecture, grading criteria, etc.), then give your final answer STRICTLY "
                         "in the JSON format requested above. Never refuse or leave it blank.)")
    else:
        lines.append("你正在分析一张 H&E 全切片。你的目标是回答关于本切片的以下问题："
                     "跨倍率(缩略图→低倍→高倍)、跨切片不同空间区域收集可见证据来作答。\n")
        if options and not has_placeholder:
            lines.append(f"问题：{prompt}\n\n选项：\n{numbered}")
        else:
            lines.append(f"问题：{prompt}")
        if options:
            lines.append("（选择题——先基于你在图像中实际看到的内容推理(形态学/空间位置/计数等)，"
                         "再从上面选项中选恰好一个并原文给出。若证据不足，也要选出最可能的那一个，切勿拒答或留空。）")
        else:
            lines.append("（先基于你在图像中实际看到的内容推理(形态学/组织结构/分级标准等)，"
                         "再严格按上面要求的 JSON 格式给出最终答案。切勿拒答或留空。）")
    return "\n\n".join(lines)


def redact_wsi_identifier(text, filename):
    """Remove the source WSI basename before text is sent to an answer LLM.

    The original pathology report artifact remains untouched for audit.  This
    only closes the deterministic report-header path by which a TCGA filename
    could otherwise reach the separate answer extractor.
    """
    if not text or not filename:
        return text or ""
    return re.sub(re.escape(os.path.basename(filename)), "[WSI filename redacted]",
                  text, flags=re.IGNORECASE)


# ───────────────────────── 答案抽取 ─────────────────────────
def extract_answer(report_text, prompt, options, lang="en", *, source_filename=None):
    """从报告中抽取该题最终答案(必须落在给定选项之一)。"""
    from agent.gpt_client import call_gemini_mcp
    report_text = redact_wsi_identifier(report_text, source_filename)
    instr = (
        "You are extracting the FINAL answer to ONE multiple-choice question from a pathology "
        "analysis report.\n"
        "The answer MUST be EXACTLY one of the given option strings (verbatim).\n"
        'Output STRICT JSON only: {"answer":"<one option string>"} — no extra text.\n\n'
        "===== REPORT =====\n" + (report_text or "")[:60000] + "\n\n"
        "===== QUESTION =====\n" + prompt + "\n"
        "===== OPTIONS =====\n" + json.dumps(options, ensure_ascii=False)
    )
    resp, err = call_gemini_mcp([{"role": "user", "content": instr}],
                                max_tokens=512, call_type="multipathqa_extract")
    if err or not resp:
        return None, f"抽取失败: {err}"
    txt = resp.strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        txt = m.group(0)
    try:
        d = json.loads(txt)
        return str(d.get("answer", "")).strip(), None
    except Exception as e:
        return None, f"抽取JSON解析失败: {e} | 原文前200: {resp[:200]}"


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower()).strip(" .。")


def match_option(pred, options):
    """把抽取到的 pred 归一到给定选项之一（精确匹配 → 包含匹配）。均不匹配时保留原文（按错误计分）。"""
    if not pred:
        return ""
    np_ = _norm(pred)
    for o in options:
        if _norm(o) == np_:
            return o
    for o in options:
        if _norm(o) and (_norm(o) in np_ or np_ in _norm(o)):
            return o
    return pred  # 落不到选项时保留原文(对分会判错，留痕)


def extract_isup(report_text):
    """PANDA 专用：从报告里抽取 isup_grade(0–5)。优先解析末尾 JSON，其次正则兜底。"""
    txt = report_text or ""
    # 倒序找最后一个含 isup_grade 的扁平 JSON 块(PANDA 要求的格式即扁平对象)
    for m in reversed(re.findall(r"\{[^{}]*\}", txt, re.S)):
        try:
            d = json.loads(m)
        except Exception:
            continue
        if "isup_grade" in d:
            try:
                return str(int(d["isup_grade"])), None
            except Exception:
                pass
    m = re.search(r"isup[ _]?grade[\"'\s:=]*([0-5])", txt, re.I)
    if m:
        return m.group(1), None
    m = re.search(r"ISUP\s*(?:grade|group)?\s*[:=]?\s*([0-5])", txt, re.I)
    if m:
        return m.group(1), None
    return None, "未能从报告解析 isup_grade"


# ───────────────────────── 单题跑批 ─────────────────────────
def run_one_question(svs_path, prompt, options, idx, lang, max_steps, base_tag,
                     *, runtime_db=None, benchmark="multipathqa", sample_id=None):
    """逐题模式：单独跑一次 agent，输出隔离到 TAG/q<idx>/attempt-<file_id>。"""
    import agent.agent_loop as AL
    from agent.agent_loop import run_agent, sanitize_filename
    from app import uploaded_files

    basename = os.path.basename(svs_path)
    file_id = str(uuid.uuid4())
    qtag = f"{base_tag}/q{idx}/attempt-{file_id}"
    os.environ["PATHCOPILOT_OUTPUT_TAG"] = qtag

    block = format_question_block(prompt, options, lang)
    AL.CLINICAL_QUESTIONS = block
    AL.CLINICAL_QUESTIONS_EN = block

    uploaded_files[file_id] = {"file_id": file_id, "filename": basename,
                               "file_path": os.path.abspath(svs_path)}
    runtime = None
    runtime_receipt = None
    socketio = MockSocketIO()
    if runtime_db:
        try:
            from evaluation_runtime import EvaluationAgentRuntime

            runtime = EvaluationAgentRuntime(
                db_path=runtime_db,
                benchmark=benchmark,
                sample_id=str(sample_id if sample_id is not None else idx),
                file_id=file_id,
                task_prompt=block,
                wsi_name=basename,
                max_steps=max_steps,
                language=lang,
            )
            runtime.start()
            socketio = runtime.wrap_socket(socketio)
        except Exception as exc:
            # Shadow persistence must never change a benchmark answer.
            runtime = None
            runtime_receipt = {
                "schema_version": 1,
                "run_state": "unavailable",
                "integrity": "degraded",
                "error_code": "RUNTIME_SETUP_FAILED",
                "error_type": type(exc).__name__,
            }

    try:
        # The established Gemini 3.1 pathology executor remains authoritative
        # and is invoked exactly once with its original arguments.
        run_agent(socketio, room=f"mpqa-{file_id}", file_id=file_id,
                  max_steps=max_steps, strategy="default", mode="ai", lang=lang)

        auto_dir = os.path.join("outputs", qtag, sanitize_filename(basename), "auto")
        rpt = os.path.join(auto_dir, "pathology_report.txt")
        if os.path.exists(rpt):
            with open(rpt, encoding="utf-8") as report_file:
                report = report_file.read()
        else:
            report = ""
        parts = report.split("=" * 60)
        body = parts[1].strip() if len(parts) > 1 else report.strip()
        if not body or len(report.strip()) < 100 or '{"selected_grids"' in report:
            raise RuntimeError("报告为空/空壳")
        if runtime is not None:
            try:
                runtime.succeed({"status": "completed", "report_found": bool(report)})
                runtime_receipt = runtime.receipt()
            except Exception as exc:
                try:
                    runtime_receipt = runtime.receipt()
                except Exception:
                    runtime_receipt = {
                        "schema_version": 1,
                        "run_id": runtime.run_id,
                        "root_task_id": runtime.root_task_id,
                    }
                runtime_receipt.update({
                    "integrity": "degraded",
                    "error_code": "RUNTIME_FINALIZATION_FAILED",
                    "error_type": type(exc).__name__,
                })
        return auto_dir, report, runtime_receipt
    except Exception as exc:
        if runtime is not None:
            try:
                runtime.fail(exc)
            except Exception:
                # Preserve the original pathology exception if Runtime
                # reconciliation also fails.
                pass
        raise


# ───────────────────────── 汇总指标 ─────────────────────────
def summarize(recs):
    """Count attempted questions with a reference, including failed predictions, in both metrics."""
    scored = [r for r in recs if r.get("gold") is not None and str(r["gold"]).strip()]
    n = len(scored)
    acc = sum(r.get("correct") == 1 for r in scored) / n if n else 0.0
    by_cls = defaultdict(lambda: [0, 0])  # gold -> [correct, total]
    for r in scored:
        by_cls[_norm(str(r["gold"]))][1] += 1
        by_cls[_norm(str(r["gold"]))][0] += int(r.get("correct") == 1)
    recalls = [c / t for c, t in by_cls.values() if t]
    bacc = sum(recalls) / len(recalls) if recalls else 0.0
    return n, acc, bacc, len(by_cls)


def main():
    ap = argparse.ArgumentParser(description="MultiPathQA 评测 — 复用 PathCopilot agent")
    ap.add_argument("--csv", default=DEF_CSV)
    ap.add_argument("--img_dir", default=DEF_IMG_DIR)
    ap.add_argument("--benchmark", default="tcga_expert_vqa",
                    choices=["tcga_expert_vqa", "tcga", "tcga_slidebench", "panda", "gtex"])
    ap.add_argument("--provider", default="openrouter", choices=["openrouter"])
    ap.add_argument("--lang", default="en", choices=["zh", "en"])
    ap.add_argument("--tag", default=None, help="输出标签(默认 MPQA-{bench}-{模型})")
    ap.add_argument("--organ", default=None, help="器官占位词（默认'未指定/unspecified'）。不要填写真实器官：器官信息不提供给模型，由模型根据图像判断。")
    ap.add_argument("--max_steps", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help=">0 只跑前 N 题(验证用)")
    ap.add_argument("--only", default="", help="只重跑指定题号(逗号分隔,如 3,7)；这些题重新运行，旧结果备份到 history/，其余题沿用已有结果")
    ap.add_argument("--no_resume", action="store_true")
    ap.add_argument("--res_dir", default=None, help="结果目录(默认按子集命名 multipathqa_results-{bench};指定则写入该独立文件夹,便于隔离重跑不污染原结果)")
    ap.add_argument("--runtime-mode", choices=["shadow", "off"], default="shadow",
                    help="Agent Runtime 模式；shadow 只做本地生命周期/审计，不参与病理决策")
    ap.add_argument("--runtime-db", default=None,
                    help="shadow Runtime SQLite 路径；默认写入结果目录，off 模式不可用")
    args = ap.parse_args()
    if args.runtime_mode == "off" and args.runtime_db:
        ap.error("--runtime-db cannot be used with --runtime-mode off")
    only_set = {s.strip() for s in args.only.split(",") if s.strip() != ""}

    os.environ["API_PROVIDER"] = args.provider
    os.environ["PATHCOPILOT_ORGAN"] = args.organ or (
        "unspecified (infer from the image)" if args.lang == "en" else "未指定（请从图像自行判断）")
    # 单题模式标记（当前代码不读取；问题由 CLINICAL_QUESTIONS 按题注入）
    os.environ["PATHCOPILOT_QA_MODE"] = "single"
    # Explicitly enable the same relaxed 15% tissue filter for global and
    # local candidate grids.  The 2.5x/5x ablation thresholds are not used.
    os.environ["PATHCOPILOT_MASK_POLICY"] = MASK_POLICY
    os.environ["PATHCOPILOT_TISSUE_THRESHOLD"] = str(TISSUE_THRESHOLD)
    os.environ["PATHCOPILOT_LOCAL_GRID_TISSUE_PREFILTER"] = "1"

    rows = load_rows(os.path.abspath(args.csv), args.benchmark)
    img_dir = os.path.abspath(args.img_dir)

    # 解析图像路径，过滤未下载的
    runnable, missing = [], []
    for r in rows:
        sp = resolve_image(img_dir, r["image_path"], r.get("file_id"))
        if sp:
            r["svs"] = sp
            runnable.append(r)
        else:
            missing.append(r["image_path"])
    if missing:
        uniq_missing = sorted(set(missing))
        print(f"[WARN] {len(uniq_missing)} 张图未下载，相关 {len(missing)} 题将跳过：{uniq_missing[:5]}")
    if args.limit > 0:
        runnable = runnable[:args.limit]

    imported_modules = activate_local_deployment()
    DEFAULT_MODEL = imported_modules[2].DEFAULT_MODEL
    agent_loop_sha256 = source_sha256(imported_modules[1].__file__)
    run_context = {
        "provider": args.provider, "model": DEFAULT_MODEL,
        "lang": args.lang, "max_steps": args.max_steps,
        "organ": os.environ["PATHCOPILOT_ORGAN"],
        "evaluator_sha256": source_sha256(__file__),
        "gpt_client_sha256": source_sha256(imported_modules[2].__file__),
    }

    tag = args.tag or f"MPQA-{args.benchmark}-{DEFAULT_MODEL.split('/')[-1].upper()}"
    os.environ["PATHCOPILOT_OUTPUT_TAG"] = tag

    _bm_dir_name = {
        "tcga": "multipathqa_results-tcga",      # 已改名为按子集命名
        "tcga_expert_vqa": "multipathqa_results-ExpertVQA",
    }
    if args.res_dir:
        res_dir = args.res_dir if os.path.isabs(args.res_dir) else os.path.join(HERE, args.res_dir)
    else:
        res_dir = os.path.join(HERE, _bm_dir_name.get(args.benchmark,
                               f"multipathqa_results-{args.benchmark}"))
    os.makedirs(res_dir, exist_ok=True)
    detail_csv = os.path.join(res_dir, f"{tag}_per_question.csv")
    if args.runtime_mode == "shadow":
        if args.runtime_db:
            runtime_db = (args.runtime_db if os.path.isabs(args.runtime_db)
                          else os.path.join(HERE, args.runtime_db))
            runtime_db = os.path.abspath(runtime_db)
        else:
            safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", tag).strip("-") or "multipathqa"
            runtime_db = os.path.join(res_dir, f"{safe_tag}_agent_runtime.sqlite3")
    else:
        runtime_db = None

    print("=" * 84)
    print(f"MultiPathQA 评测 | 子集: {args.benchmark} | 模型: {DEFAULT_MODEL} | "
          f"provider: {args.provider} | lang: {args.lang}")
    print(f"可跑题数: {len(runnable)} | 器官中立词: {os.environ['PATHCOPILOT_ORGAN']} | TAG: {tag}")
    print(f"输出: outputs/{tag}/  明细: {detail_csv}")
    print("模块来源: " + " | ".join(
        f"{m.__name__}={os.path.realpath(m.__file__)}"
        for m in imported_modules
    ))
    print("候选框策略: primary 10x/40x/160x | "
          "relaxed_gray_std_no_morphology_v3_tissue15_fail_closed | 15%")
    print(f"agent_loop.py SHA-256: {agent_loop_sha256}")
    print(f"Agent Runtime: {args.runtime_mode}" +
          (f" | SQLite: {runtime_db}" if runtime_db else ""))
    print("=" * 84)

    all_recs = []
    for n, r in enumerate(runnable, 1):
        idx = r["bid"] if str(r["bid"]).strip() != "" else n
        fingerprint = evaluation_fingerprint(r, idx, run_context)
        ans_json = os.path.join(res_dir, f"{tag}__{args.benchmark}__q{idx}.json")
        # --only: 指定题号重新运行（旧结果备份到 history/）；其余题沿用已有结果
        force = bool(only_set) and str(idx) in only_set
        if only_set and not force:
            if os.path.exists(ans_json):
                rec = json.load(open(ans_json, encoding="utf-8")).get("rec")
                if resume_record_is_compatible(rec, agent_loop_sha256, fingerprint):
                    all_recs.append(rec)
                else:
                    raise ValueError(
                        f"q{idx} 旧结果缺少校验信息或与本次输入/配置不兼容：{ans_json}。"
                        "已停止复用并保留旧文件；请使用新的 --res_dir，或将该题加入 --only 显式重跑。"
                    )
            # 无旧结果也直接跳过（防止换错 tag 时全量重跑）
            continue
        if not force and not args.no_resume and os.path.exists(ans_json):
            saved = json.load(open(ans_json, encoding="utf-8"))
            rec = saved.get("rec")
            if not resume_record_is_compatible(rec, agent_loop_sha256, fingerprint):
                raise ValueError(
                    f"q{idx} 旧结果缺少校验信息或与本次输入/配置不兼容：{ans_json}。"
                    "已停止复用并保留旧文件；请使用新的 --res_dir、--only 或 --no_resume 显式重跑。"
                )
            if not rec.get("error"):
                print(f"[{n}/{len(runnable)}] ⏭️  跳过(当前协议已完成) q{idx}")
                all_recs.append(rec)
                continue
            print(f"[{n}/{len(runnable)}] 重试上次失败的 q{idx}")
        if force:
            print(f"[{n}/{len(runnable)}] 🔁 重跑(覆盖) q{idx}")

        print(f"[{n}/{len(runnable)}] ▶️  q{idx} | {os.path.basename(r['svs'])[:32]} | {r['prompt'][:60]}")
        t0 = time.time()
        auto_dir, runtime_receipt, error = "", None, ""
        try:
            auto_dir, report, runtime_receipt = run_one_question(
                r["svs"], r["prompt"], r["options"], idx, args.lang,
                args.max_steps, tag, runtime_db=runtime_db,
                benchmark=args.benchmark, sample_id=idx)
            if not report or len(report.strip()) < 100:
                raise RuntimeError("报告为空/空壳")
            if args.benchmark == "panda":
                # PANDA: 无选项，直接抽 ISUP 等级并按数值对分
                pred_raw, eerr = extract_isup(report)
                if eerr:
                    raise RuntimeError(eerr)
                pred = pred_raw
                correct = int(str(pred).strip() == str(r["gold"]).strip()) \
                    if str(r["gold"]).strip() != "" else ""
            else:
                pred_raw, eerr = extract_answer(
                    report, r["prompt"], r["options"], args.lang,
                    source_filename=os.path.basename(r["svs"]))
                if eerr or not str(pred_raw or "").strip():
                    raise RuntimeError(eerr or "答案抽取为空")
                pred = match_option(pred_raw, r["options"])
                correct = int(_norm(pred) == _norm(r["gold"])) if r["gold"] else ""
        except Exception as e:
            traceback.print_exc()
            error = f"{type(e).__name__}: {e}"
            pred_raw = pred = ""
            correct = 0 if r["gold"] else ""
            print(f"        ✗ {error[:80]}  ({time.time()-t0:.0f}s)")

        rec = {
            "benchmark": r["benchmark"], "idx": idx, "image": os.path.basename(r["svs"]),
            "question": r["prompt"], "options": r["options"],
            "gold": r["gold"], "pred": pred, "pred_raw": pred_raw,
            "correct": correct, "error": error, "metric_type": r["metric_type"], "auto_dir": auto_dir,
            "view_profile": VIEW_PROFILE,
            "tissue_mask_policy": MASK_POLICY,
            "tissue_content_threshold": TISSUE_THRESHOLD,
            "protocol_revision": PROTOCOL_REVISION,
            "agent_loop_sha256": agent_loop_sha256,
            "evaluation_fingerprint": fingerprint,
            "evaluation_context": run_context,
        }
        result_payload = {"rec": rec}
        if runtime_receipt is not None:
            result_payload["agent_runtime"] = runtime_receipt
        if os.path.exists(ans_json):
            history_dir = os.path.join(res_dir, "history")
            os.makedirs(history_dir, exist_ok=True)
            history_name = (f"{os.path.splitext(os.path.basename(ans_json))[0]}__"
                            f"{time.strftime('%Y%m%d_%H%M%S')}__{uuid.uuid4().hex}.json")
            history_path = os.path.join(history_dir, history_name)
            with open(ans_json, "rb") as previous_file, open(history_path, "xb") as history_file:
                history_file.write(previous_file.read())
            print(f"        旧评分已备份: {history_path}")
        with tempfile.TemporaryDirectory(dir=res_dir) as staging:
            pending = os.path.join(staging, "result.json")
            with open(pending, "w", encoding="utf-8") as result_file:
                json.dump(result_payload, result_file, ensure_ascii=False, indent=2)
            os.replace(pending, ans_json)
        tip = "✓ 正确" if correct == 1 else ("✗ 错" if correct == 0 else "(未对分)")
        print(f"        {tip}  gold='{r['gold']}'  pred='{pred}'  ({time.time()-t0:.0f}s)")
        all_recs.append(rec)

    # 写明细
    if all_recs:
        cols = ["benchmark", "idx", "image", "metric_type", "view_profile",
                "tissue_mask_policy", "tissue_content_threshold",
                "protocol_revision", "agent_loop_sha256", "question",
                "gold", "pred", "pred_raw", "correct", "options", "error"]
        with open(detail_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in all_recs:
                row = dict(r)
                row["options"] = " | ".join(r.get("options", []))
                w.writerow({c: row.get(c, "") for c in cols})

    n, acc, bacc, ncls = summarize(all_recs)
    metric = all_recs[0]["metric_type"] if all_recs else "accuracy"
    print("=" * 84)
    failed = sum(bool(r.get("error")) and r.get("correct") == 0 for r in all_recs)
    print(f"完成。已对分 {n} 题（含失败计错 {failed} 题）| 未对分 {len(all_recs) - n} 题 | 类别数 {ncls}")
    print(f"  Accuracy          = {acc*100:.1f}%")
    print(f"  Balanced Accuracy = {bacc*100:.1f}%   ← 本子集官方指标: {metric}")
    print(f"明细表: {detail_csv}")


if __name__ == "__main__":
    main()
