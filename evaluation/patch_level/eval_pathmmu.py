#!/usr/bin/env python3
"""
PathMMU Evaluation Script

Evaluate multimodal LLMs on the PathMMU benchmark via the OpenRouter API.
Supports checkpoint-based resumption, logging, and XLSX export.

Usage:
    python eval_pathmmu.py --category pdv --model openai/gpt-5.5 --limit 50
    python eval_pathmmu.py --category val
    python eval_pathmmu.py --category test_tiny --no_resume
"""

import requests
import base64
import json
import hashlib
import os
import sys
import time
import re
import io
import argparse
import traceback
from PIL import Image
from tqdm import tqdm
from datetime import datetime


# ======================== Logging Utilities ========================

class Tee:
    """Redirect stdout to both console and a log file simultaneously."""

    def __init__(self, *files):
        self.files = files
        self.encoding = "utf-8"

    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()

    def flush(self):
        for f in self.files:
            f.flush()


class Logger:
    """Session-level logger that mirrors stdout into a timestamped file."""

    def __init__(self, log_dir, model_name, category):
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(
            log_dir, f"{model_name}_{category}_{timestamp}.log"
        )
        self.file_handle = open(self.log_file, "a", encoding="utf-8")
        self.original_stdout = sys.stdout
        self.tee = Tee(sys.stdout, self.file_handle)
        sys.stdout = self.tee
        print(f"[+] Log file: {self.log_file}")

    def close(self):
        sys.stdout = self.original_stdout
        self.file_handle.close()


# ======================== API Configuration ========================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not OPENROUTER_API_KEY:
    raise EnvironmentError(
        "Please set the OPENROUTER_API_KEY environment variable.\n"
        "  export OPENROUTER_API_KEY=sk-or-v1-xxxx"
    )

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
}

DEFAULT_MODEL = "openai/gpt-5.5"

# ======================== Dataset Category Mapping ========================

CAT_SHORT2LONG = {
    "pdtt": "PubMed_test_tiny",
    "pdt": "PubMed_test",
    "pdv": "PubMed_val",
    "clstt": "PathCLS_test_tiny",
    "clst": "PathCLS_test",
    "clsv": "PathCLS_val",
    "sptt": "SocialPath_test_tiny",
    "spt": "SocialPath_test",
    "spv": "SocialPath_val",
    "at": "Atlas_test",
    "att": "Atlas_test_tiny",
    "av": "Atlas_val",
    "edutt": "EduContent_test_tiny",
    "edut": "EduContent_test",
    "eduv": "EduContent_val",
    "quick": "PubMed_val",
}

COMBINED_CATEGORIES = {
    "val": [
        "PubMed_val", "SocialPath_val", "EduContent_val",
        "PathCLS_val", "Atlas_val",
    ],
    "test": [
        "PubMed_test", "SocialPath_test", "EduContent_test",
        "PathCLS_test", "Atlas_test",
    ],
    "test_tiny": [
        "PubMed_test_tiny", "SocialPath_test_tiny", "EduContent_test_tiny",
        "PathCLS_test_tiny", "Atlas_test_tiny",
    ],
}


# ======================== Image Encoding ========================

def encode_image(image_path: str, compress: bool = False) -> str | None:
    """Encode an image file to a base64 string.

    Args:
        image_path: Path to the image file.
        compress: Whether to resize/compress (for large images; PathMMU
                  patches are already small, so default is False).
    """
    try:
        if not (image_path and os.path.exists(image_path)):
            return None

        if compress:
            img = Image.open(image_path)
            if img.mode != "RGB":
                img = img.convert("RGB")
            max_size = 1024
            if max(img.size) > max_size:
                ratio = max_size / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=90)
            return base64.b64encode(buffer.getvalue()).decode("utf-8")

        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    except Exception as e:
        print(f"[!] Image encoding failed for {image_path}: {e}")
    return None


# ======================== API Call ========================

def call_vision_api(
    image_path: str,
    question: str,
    options: list[str],
    model: str = DEFAULT_MODEL,
    max_retries: int = 3,
) -> tuple[str | None, str | None, str | None]:
    """Send an image + question to the OpenRouter vision API.

    Returns:
        (answer_letter, full_response_text, error_message)
    """
    encoded_image = encode_image(image_path, compress=False)
    if not encoded_image:
        return None, None, "Image encoding failed"

    ext = os.path.splitext(image_path)[1].lower()
    mime_type = "image/png" if ext == ".png" else "image/jpeg"

    options_text = "\n".join(options)
    valid_choices = [chr(ord("A") + i) for i in range(len(options))]
    prompt = (
        "You are a professional pathologist specializing in histopathological "
        "image interpretation.\n\n"
        "Carefully examine the given pathology image, identify key tissue and "
        "cellular features, and select the most appropriate answer based on "
        "your observation.\n\n"
        f"**Question:**\n{question}\n\n"
        f"**Options:**\n{options_text}\n\n"
        "**Response Format:**\n"
        "<thinking>\n"
        "Describe the image features you observe and explain your reasoning.\n"
        "</thinking>\n\n"
        "<answer>X</answer>\n\n"
        f"Note: Only provide a single letter ({'/'.join(valid_choices)}) as the answer."
    )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a professional pathologist specializing in "
                    "histopathological image interpretation. Carefully examine "
                    "the given pathology image, identify key tissue and cellular "
                    "features, and select the most appropriate answer based on "
                    "your observation."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{encoded_image}"
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            },
        ],
        "max_tokens": 16000,
        "temperature": 0.2,
    }

    print(f"\n  [INFO] Model: {model}")
    print(f"  [INFO] Image: {os.path.basename(image_path)}")
    print(f"  [INFO] Question: {question}")

    error = None
    for attempt in range(max_retries):
        try:
            print(f"  [INFO] Sending request... (attempt {attempt + 1}/{max_retries})")
            response = requests.post(
                OPENROUTER_API_URL,
                headers=OPENROUTER_HEADERS,
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            result = response.json()
            full_response = result["choices"][0]["message"]["content"].strip()

            # Parse structured output
            thinking = ""
            thinking_match = re.search(
                r"<thinking>(.*?)</thinking>", full_response, re.DOTALL | re.IGNORECASE
            )
            if thinking_match:
                thinking = thinking_match.group(1).strip()

            answer = ""
            answer_match = re.search(
                r"<answer>\s*([A-Za-z])\s*</answer>", full_response, re.IGNORECASE
            )
            if answer_match and answer_match.group(1).upper() in valid_choices:
                answer = answer_match.group(1).upper()
            else:
                answer = full_response

            # Display reasoning
            print(f"\n  {'─' * 50}")
            print("  [Thinking]")
            print(f"  {'─' * 50}")
            if thinking:
                for line in thinking.split("\n"):
                    if line.strip():
                        print(f"  {line.strip()}")
            else:
                print(f"  {full_response[:500]}...")
            print(f"  {'─' * 50}")
            print(f"  [Answer]: {answer}")
            print(f"  {'─' * 50}\n")

            usage = result.get("usage", {})
            print(
                f"  [INFO] Tokens: prompt={usage.get('prompt_tokens', '?')}, "
                f"completion={usage.get('completion_tokens', '?')}"
            )
            return answer, full_response, None

        except requests.exceptions.Timeout:
            error = f"Request timeout (attempt {attempt + 1}/{max_retries})"
            print(f"  [!] {error}")
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))

        except requests.exceptions.RequestException as e:
            error = f"Request failed: {e}"
            print(f"  [!] {error}")
            if hasattr(e, "response") and e.response is not None:
                print(f"  [DEBUG] Status: {e.response.status_code}")
                print(f"  [DEBUG] Body: {e.response.text[:500]}")
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))

        except Exception as e:
            error = f"Unexpected error: {e}"
            print(f"  [!] {error}")
            traceback.print_exc()
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))

    return None, None, error


# ======================== Answer Extraction ========================

ALL_CHOICES = ["A", "B", "C", "D"]


def extract_prediction(response: str | None, valid_choices: list[str] | None = None) -> str | None:
    """Extract an explicit, unambiguous option; return None on parsing failure."""
    if not response:
        return None

    text = response.strip().upper()
    allowed_choices = set(ALL_CHOICES if valid_choices is None else valid_choices)

    tagged = re.findall(r"<ANSWER>\s*(.*?)\s*</ANSWER>", text, re.DOTALL)
    if tagged:
        values = [re.fullmatch(r"\(?([A-Z])\)?[.!。]?", value.strip(" \t\r\n*_`\"'")) for value in tagged]
        if not all(values):
            return None
        choices = {value.group(1) for value in values}
        return next(iter(choices)) if len(choices) == 1 and choices <= allowed_choices else None

    for marker in (r"\bFINAL\s+(?:ANSWER|CHOICE)\b|最终(?:答案|选择)",
                   r"\b(?:CORRECT\s+)?ANSWER\b|答案"):
        matches = list(re.finditer(
            rf"(?:{marker})\s*(?:IS\b|为|是)?\s*[:：=]?\s*"
            r"(?:OPTION\s+|CHOICE\s+|选项\s*)?[*_`\"'\s]*\(?([A-Z])\)?(?![A-Z0-9])",
            text,
        ))
        if matches:
            choices = {match.group(1) for match in matches}
            ambiguous = any(re.match(
                r"\s*(?:OR\b|AND\b|[/,，、]|或(?:者|是)?|和)\s*\(?[A-Z]\)?(?![A-Z0-9])",
                text[match.end():],
            ) for match in matches)
            return next(iter(choices)) if not ambiguous and len(choices) == 1 and choices <= allowed_choices else None

    text = text.strip(" \t\r\n*_`\"'")
    bare = re.fullmatch(r"\(?([A-Z])\)?[.!。]?", text)
    if bare:
        return bare.group(1) if bare.group(1) in allowed_choices else None

    # Preserve a leading labelled answer such as "B) ...", but not an option list.
    labels = list(re.finditer(r"\(([A-Z])\)|(?<![A-Z0-9])([A-Z])[).:：、]", text))
    if labels and labels[0].start() == 0:
        choices = {match.group(1) or match.group(2) for match in labels}
        return next(iter(choices)) if len(choices) == 1 and choices <= allowed_choices else None
    return None


# ======================== Data Loading ========================

def load_pathmmu_data(data_path: str, category: str) -> list[dict]:
    """Load and validate PathMMU samples for the given category.

    Args:
        data_path: Root directory containing ``data.json`` and ``images/``.
        category: Full category name, e.g. ``PubMed_val``.

    Returns:
        List of sample dicts with keys: No, question, options, answer,
        img_path, explanation.
    """
    json_path = os.path.join(data_path, "data.json")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    parts = category.split("_")
    main_cat = parts[0]
    split = "_".join(parts[1:])

    if main_cat not in data:
        raise ValueError(
            f"Category '{main_cat}' not found. Available: {list(data.keys())}"
        )
    if split not in data[main_cat]:
        raise ValueError(
            f"Split '{split}' not found. Available: {list(data[main_cat].keys())}"
        )

    samples = data[main_cat][split]
    images_dir = os.path.join(data_path, "images")
    processed = []

    for sample in samples:
        img_path = os.path.join(images_dir, sample["img"])
        processed.append(
            {
                "No": sample["No"],
                "question": sample["question"],
                "options": sample["options"],
                "answer": sample["answer"],
                "img_path": img_path,
                "explanation": sample.get("explanation", ""),
            }
        )

    print(f"[+] Loaded {category}: {len(processed)}/{len(samples)} samples")
    return processed


# ======================== Evaluation Metrics ========================

def compute_metrics(results: list[dict]) -> dict:
    """Compute accuracy over a list of result dicts."""
    correct = 0
    for r in results:
        gt = r["gt_answer"][0].upper()
        pred = r["pred_answer"].upper() if r["pred_answer"] else ""
        is_correct = gt == pred
        r["correct"] = is_correct
        correct += int(is_correct)

    total = len(results)
    return {"accuracy": correct / total if total else 0, "correct": correct, "total": total}


# ======================== Checkpointing ========================

def _checkpoint_path(output_dir: str, category: str, model: str) -> str:
    safe_model = model.replace("/", "_").replace(":", "_")
    return os.path.join(output_dir, f".checkpoint_{category}_{safe_model}.json")


def checkpoint_fingerprint(samples: list[dict], category: str, model: str) -> str:
    """Bind a checkpoint to the selected records, image contents and evaluator."""
    with open(__file__, "r", encoding="utf-8-sig") as source:
        code_hash = hashlib.sha256(source.read().encode("utf-8")).hexdigest()
    records = []
    for sample in samples:
        image_hash = hashlib.sha256()
        with open(sample["img_path"], "rb") as image:
            for chunk in iter(lambda: image.read(1024 * 1024), b""):
                image_hash.update(chunk)
        item = {key: value for key, value in sample.items() if key != "img_path"}
        item["image_sha256"] = image_hash.hexdigest()
        item["image_suffix"] = os.path.splitext(sample["img_path"])[1].lower()
        records.append(item)
    payload = {"category": category, "model": model, "records": records, "code_sha256": code_hash}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def load_checkpoint(path: str, expected_fingerprint: str) -> tuple[list, set]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("fingerprint") != expected_fingerprint:
                raise ValueError("missing or incompatible checkpoint fingerprint")
            results, completed = data["results"], data["completed_nos"]
            required = {"No", "question", "options", "gt_answer", "pred_answer", "raw_response", "error", "correct"}
            if (not isinstance(results, list) or not isinstance(completed, list)
                    or any(not isinstance(r, dict) or not required <= r.keys() for r in results)):
                raise ValueError("invalid checkpoint record structure")
            result_ids = [r["No"] for r in results]
            if (len(result_ids) != len(set(result_ids)) or len(completed) != len(set(completed))
                    or set(result_ids) != set(completed)):
                raise ValueError("duplicate or inconsistent completed question IDs")
            print(f"[+] Checkpoint found: {path}")
            print(f"    Completed: {len(results)} samples")
            return results, set(completed)
        except Exception as e:
            raise ValueError(
                f"Cannot safely resume checkpoint {path}: {e}. "
                "Checkpoint left unchanged; use a new --output_dir for a fresh evaluation."
            ) from e
    return [], set()


def save_checkpoint(path: str, results: list, completed_nos: set, fingerprint: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "results": results,
                "completed_nos": list(completed_nos),
                "last_update": datetime.now().isoformat(),
                "fingerprint": fingerprint,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def delete_checkpoint(path: str):
    if os.path.exists(path):
        os.remove(path)
        print(f"[+] Checkpoint deleted: {path}")


# ======================== XLSX Export ========================

def save_results_to_xlsx(
    results: list[dict],
    metrics: dict,
    category: str,
    model: str,
    output_dir: str,
) -> str | None:
    """Export evaluation results to an Excel workbook."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    except ImportError:
        print("[!] openpyxl not installed — skipping XLSX export. "
              "Install via: pip install openpyxl")
        return None

    wb = Workbook()
    ws = wb.active
    ws.title = "Results"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    correct_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    wrong_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    ws["A1"] = "Evaluation Summary"
    ws["A1"].font = Font(bold=True, size=14)
    ws.merge_cells("A1:E1")

    ws["A2"], ws["B2"] = "Model", model
    ws["A3"], ws["B3"] = "Category", category
    ws["A4"], ws["B4"] = "Accuracy", f"{metrics['accuracy'] * 100:.2f}%"
    ws["A5"], ws["B5"] = "Correct / Total", f"{metrics['correct']}/{metrics['total']}"

    headers = ["No.", "Question", "Options", "Ground Truth", "Prediction", "Correct", "Reasoning"]
    start_row = 7
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for i, r in enumerate(results, 1):
        row = start_row + i
        ws.cell(row=row, column=1, value=r["No"]).border = thin_border
        ws.cell(row=row, column=2, value=r["question"][:100]).border = thin_border
        ws.cell(row=row, column=3, value="\n".join(r["options"])).border = thin_border
        ws.cell(row=row, column=4, value=r["gt_answer"]).border = thin_border
        ws.cell(row=row, column=5, value=r["pred_answer"]).border = thin_border

        is_correct = r.get("correct", False)
        c = ws.cell(row=row, column=6, value="Y" if is_correct else "N")
        c.fill = correct_fill if is_correct else wrong_fill
        c.border = thin_border
        c.alignment = Alignment(horizontal="center")

        raw = (r.get("raw_response") or "")[:500]
        ws.cell(row=row, column=7, value=raw).border = thin_border

    for col_letter, width in zip("ABCDEFG", [10, 50, 40, 12, 12, 10, 60]):
        ws.column_dimensions[col_letter].width = width

    safe_model = model.replace("/", "_").replace(":", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx_path = os.path.join(output_dir, f"{safe_model}_{category}_{timestamp}.xlsx")
    wb.save(xlsx_path)
    print(f"[+] XLSX saved: {xlsx_path}")
    return xlsx_path


# ======================== Main Evaluation Loop ========================

def run_evaluation(
    data_path: str,
    category: str,
    model: str = DEFAULT_MODEL,
    limit: int | None = None,
    output_dir: str = "outputs",
    resume: bool = True,
) -> tuple[dict, list]:
    """Run the full evaluation pipeline for a single category."""
    print(f"\n{'=' * 60}")
    print(f"PathMMU Evaluation")
    print(f"  Category : {category}")
    print(f"  Model    : {model}")
    print(f"  Resume   : {'enabled' if resume else 'disabled'}")
    print(f"{'=' * 60}\n")

    os.makedirs(output_dir, exist_ok=True)
    samples = load_pathmmu_data(data_path, category)

    if limit:
        samples = samples[:limit]
        print(f"[!] Sample limit applied: {limit}")

    missing_images = [s for s in samples if not os.path.isfile(s["img_path"])]
    if missing_images:
        details = "\n".join(f"  {s['No']}: {s['img_path']}" for s in missing_images)
        raise FileNotFoundError(
            f"Missing image files for {len(missing_images)}/{len(samples)} selected questions in {category}:\n"
            f"{details}\nEvaluation stopped before model calls. Restore the missing images and rerun."
        )

    fingerprint = checkpoint_fingerprint(samples, category, model)
    ckpt_path = _checkpoint_path(output_dir, category, model)
    results: list[dict] = []
    completed_nos: set = set()

    if resume:
        results, completed_nos = load_checkpoint(ckpt_path, fingerprint)
        if not completed_nos <= {s["No"] for s in samples}:
            raise ValueError("Checkpoint contains questions outside this evaluation; use a new --output_dir.")
        if completed_nos:
            print(f"[+] Resuming — skipping {len(completed_nos)} completed samples")

    pending = [s for s in samples if s["No"] not in completed_nos]

    if not pending:
        print("[+] All samples already evaluated!")
    else:
        print(f"\nEvaluating {len(pending)} samples (total {len(samples)})...\n")

    for sample in tqdm(pending, desc="Evaluation"):
        answer, full_response, error = call_vision_api(
            sample["img_path"],
            sample["question"],
            sample["options"],
            model=model,
        )

        if answer or full_response is not None:
            pred = extract_prediction(
                full_response or answer,
                valid_choices=[chr(ord("A") + i) for i in range(len(sample["options"]))],
            )
            if pred is None:
                error = error or "Answer parsing failed: no unambiguous valid option"
                print(f"  [!] Sample {sample['No']}: {error} — scored as incorrect")
        else:
            pred = None
            print(f"  [!] Sample {sample['No']} failed: {error} — scored as incorrect")

        gt_letter = sample["answer"][0].upper()
        is_correct = pred is not None and pred.upper() == gt_letter

        result = {
            "No": sample["No"],
            "question": sample["question"],
            "options": sample["options"],
            "gt_answer": sample["answer"],
            "pred_answer": pred,
            "raw_response": full_response,
            "error": error,
            "correct": is_correct,
        }
        results.append(result)
        completed_nos.add(sample["No"])
        save_checkpoint(ckpt_path, results, completed_nos, fingerprint)

        status = "CORRECT" if is_correct else "WRONG"
        print(f"  [Result] GT={gt_letter} | Pred={pred} | {status}")
        print(f"  {'=' * 50}\n")

    metrics = compute_metrics(results)

    print(f"\n{'=' * 60}")
    print("Evaluation Results")
    print(f"{'=' * 60}")
    print(f"  Accuracy : {metrics['accuracy'] * 100:.2f}%")
    print(f"  Correct  : {metrics['correct']} / {metrics['total']}")
    print(f"{'=' * 60}\n")

    # Save JSON results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = model.replace("/", "_").replace(":", "_")
    json_path = os.path.join(output_dir, f"{safe_model}_{category}_{timestamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {"category": category, "model": model, "metrics": metrics, "results": results},
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"[+] JSON saved: {json_path}")

    save_results_to_xlsx(results, metrics, category, model, output_dir)
    delete_checkpoint(ckpt_path)

    return metrics, results


# ======================== CLI Entry Point ========================

def main():
    parser = argparse.ArgumentParser(
        description="Evaluate multimodal LLMs on the PathMMU benchmark."
    )
    parser.add_argument(
        "--data_path", type=str, default=".",
        help="Root directory of PathMMU data (containing data.json and images/).",
    )
    parser.add_argument(
        "--category", type=str, default="quick",
        help=(
            f"Evaluation subset. Single: {list(CAT_SHORT2LONG.keys())}; "
            f"Combined: val / test / test_tiny."
        ),
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL,
        help="Model identifier on OpenRouter (e.g. openai/gpt-5.5).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of samples to evaluate (for quick testing).",
    )
    parser.add_argument(
        "--output_dir", type=str, default="outputs",
        help="Directory for output files.",
    )
    parser.add_argument(
        "--no_resume", action="store_true",
        help="Disable checkpoint resumption (start from scratch).",
    )
    args = parser.parse_args()

    if args.category in COMBINED_CATEGORIES:
        log_category = args.category
    elif args.category in CAT_SHORT2LONG:
        log_category = CAT_SHORT2LONG[args.category]
    else:
        log_category = args.category

    logger = Logger(args.output_dir, args.model.replace("/", "_"), log_category)

    try:
        if args.category in COMBINED_CATEGORIES:
            categories = COMBINED_CATEGORIES[args.category]
            print(f"\n{'=' * 60}")
            print(f"Combined evaluation mode: {args.category}")
            print(f"  Subsets: {categories}")
            print(f"{'=' * 60}\n")

            all_results = []
            total_correct = 0
            total_count = 0

            for cat in categories:
                print(f"\n>>> Evaluating: {cat}")
                metrics, results = run_evaluation(
                    data_path=args.data_path,
                    category=cat,
                    model=args.model,
                    limit=args.limit,
                    output_dir=args.output_dir,
                    resume=not args.no_resume,
                )
                all_results.extend(results)
                total_correct += metrics["correct"]
                total_count += metrics["total"]

            overall_acc = total_correct / total_count * 100 if total_count else 0
            print(f"\n{'=' * 60}")
            print("Combined Evaluation Summary")
            print(f"{'=' * 60}")
            print(f"  Overall accuracy : {overall_acc:.2f}%")
            print(f"  Correct / Total  : {total_correct} / {total_count}")
            print(f"{'=' * 60}\n")

            combined_metrics = {
                "accuracy": total_correct / total_count if total_count else 0,
                "correct": total_correct,
                "total": total_count,
            }
            save_results_to_xlsx(
                all_results, combined_metrics, args.category, args.model, args.output_dir
            )
        else:
            category = CAT_SHORT2LONG.get(args.category, args.category)
            run_evaluation(
                data_path=args.data_path,
                category=category,
                model=args.model,
                limit=args.limit,
                output_dir=args.output_dir,
                resume=not args.no_resume,
            )
    finally:
        logger.close()


if __name__ == "__main__":
    main()
