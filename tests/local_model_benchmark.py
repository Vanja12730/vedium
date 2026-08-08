# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Benchmark local GGUF models against Vedium's reading/writing prompts.

This is intentionally an end-to-end llama-server benchmark.  It uses the same
semantic system/user messages, sampling defaults, repeat penalties, and
``enable_thinking=False`` setting as Vedium's managed-server path.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vedium.constants import _REPHRASE_PROMPTS  # noqa: E402
from vedium.llm import (  # noqa: E402
    _answer_language_instruction,
    _language_instruction,
)
from vedium.llm_postprocess import _clean_llm_output, _strip_reasoning  # noqa: E402


DEFAULT_SERVER = Path(r"D:\LLM\llama-b9888-bin-win-cuda-13.3-x64\llama-server.exe")
MODEL_ROOT = Path(r"D:\LLM\models")
OUT_DIR = ROOT / "Output" / "local_model_benchmark"


MODELS = {
    "qwen35-08b": {
        "name": "Qwen3.5 0.8B Q8_0",
        "path": MODEL_ROOT / r"lmstudio-community\Qwen3.5-0.8B-GGUF\Qwen3.5-0.8B-Q8_0.gguf",
    },
    "gemma3-4b": {
        "name": "Gemma 3 4B Q4_K_M",
        "path": MODEL_ROOT / r"lmstudio-community\gemma-3-4b-it-GGUF\gemma-3-4b-it-Q4_K_M.gguf",
    },
    "qwen3-4b": {
        "name": "Qwen3 4B Instruct 2507 Q4_K_M",
        "path": MODEL_ROOT / r"chaparro2001\Qwen3-4B-Instruct-2507-Q4_K_M-GGUF\qwen3-4b-instruct-2507-q4_k_m.gguf",
    },
    "bonsai-27b": {
        "name": "Bonsai 27B Q1_0",
        "path": MODEL_ROOT / r"lmstudio-community\Bonsai-27B-GGUF\Bonsai-27B-Q1_0.gguf",
    },
    "gemma4-e4b": {
        "name": "Gemma 4 E4B Q4_K_M",
        "path": MODEL_ROOT / r"lmstudio-community\gemma-4-E4B-it-GGUF\gemma-4-E4B-it-Q4_K_M.gguf",
    },
    "qwen25-7b": {
        "name": "Qwen2.5 7B Instruct 1M Q4_K_M",
        "path": MODEL_ROOT / r"lmstudio-community\Qwen2.5-7B-Instruct-1M-GGUF\Qwen2.5-7B-Instruct-1M-Q4_K_M.gguf",
    },
    "qwen35-9b": {
        "name": "Qwen3.5 9B Q4_K_M",
        "path": MODEL_ROOT / r"lmstudio-community\Qwen3.5-9B-GGUF\Qwen3.5-9B-Q4_K_M.gguf",
    },
    "gemma4-12b": {
        "name": "Gemma 4 12B Q4_K_M",
        "path": MODEL_ROOT / r"lmstudio-community\gemma-4-12B-it-GGUF\gemma-4-12B-it-Q4_K_M.gguf",
        "cache": "q4_0",
        "batch": 1024,
        "ubatch": 256,
    },
    "gptoss-20b": {
        "name": "gpt-oss 20B MXFP4 (n-cpu-moe 12)",
        "path": MODEL_ROOT / r"lmstudio-community\gpt-oss-20b-GGUF\gpt-oss-20b-MXFP4.gguf",
        "extra": ["--n-cpu-moe", "12"],
        "batch": 512,
        "ubatch": 256,
    },
    "gemma4-26b": {
        "name": "Gemma 4 26B A4B Q4_K_M (n-cpu-moe 22)",
        "path": MODEL_ROOT / r"lmstudio-community\gemma-4-26B-A4B-it-GGUF\gemma-4-26B-A4B-it-Q4_K_M.gguf",
        "extra": ["--n-cpu-moe", "22", "--no-mmap"],
        "batch": 512,
        "ubatch": 256,
    },
}


TASKS = [
    {
        "id": "read_summary",
        "category": "reading",
        "mode": "What's This About?",
        "language": "English",
        "text": (
            "During last summer's drought, Alder City offered households a rebate for replacing "
            "lawns with drought-tolerant plants. Water use among participating homes fell by 18%, "
            "but enrolment was much lower in low-income districts because residents had to pay the "
            "installation cost before being reimbursed. The council is now considering upfront "
            "vouchers, although the pilot did not measure industrial water use."
        ),
        "criteria": [
            [r"Alder City|city"],
            [r"rebate|incentive"],
            [r"drought[- ]tolerant|replace.*lawn|lawn.*replace"],
            [r"18\s*%|18 percent"],
            [r"low[- ]income|upfront cost|pay.*before|reimburs"],
            [r"upfront voucher"],
        ],
        "format": "sentences_3",
    },
    {
        "id": "read_takeaways",
        "category": "reading",
        "mode": "Key Takeaways",
        "language": "English",
        "text": (
            "Researchers tracked 240 commuters who chose to use an e-bike at least three days a "
            "week. After six months, their car trips had fallen by 22% and their weekly exercise "
            "had increased, while public-transport use showed no significant change. Because the "
            "participants selected themselves and the study excluded winter, the results cannot "
            "establish year-round causal effects."
        ),
        "criteria": [
            [r"240"], [r"three days|3 days"], [r"six months|6 months"],
            [r"22\s*%|22 percent"], [r"exercise.*increas|increas.*exercise"],
            [r"no significant change.*public|public.*no significant change|transit.*no significant"],
            [r"self[- ]select|selected themselves"], [r"winter"],
            [r"caus|year[- ]round"],
        ],
        "format": "bullets_5",
    },
    {
        "id": "read_define",
        "category": "reading",
        "mode": "Define This",
        "language": "English",
        "text": "retrieval-augmented generation",
        "criteria": [
            [r"artificial intelligence|\bAI\b|language model|\bLLM"],
            [r"retriev|look.*up|search"],
            [r"external|outside|document|knowledge base|source"],
            [r"generat|answer|response"],
            [r"ground|accur|hallucin|up[- ]to[- ]date|relevant"],
        ],
        "format": "sentences_3",
    },
    {
        "id": "read_explain_simple",
        "category": "reading",
        "mode": "Explain It (Simply)",
        "language": "English",
        "text": (
            "A diagnostic test with 95% sensitivity correctly identifies 95 of every 100 people "
            "who have the disease; this figure alone does not reveal how likely a positive result "
            "is to be correct, because that also depends on specificity and how common the disease "
            "is in the tested population."
        ),
        "criteria": [
            [r"95.*100|100.*95"], [r"have the disease|with the disease|who.*disease"],
            [r"positive.*correct|positive.*actually|positive result.*disease"],
            [r"specificity"], [r"common|prevalence"],
        ],
    },
    {
        "id": "write_polish",
        "category": "writing",
        "mode": "Polish My Writing",
        "language": "English",
        "text": (
            "Our team have reviewed the three proposals, and we think option B are the most "
            "practical. It cost €12,400, keeps the apiVersion field unchanged, and can be deployed "
            "by 17 March. However the supplier have not confirmed whether "
            "https://example.com/v2 will remain available."
        ),
        "criteria": [
            [r"team has reviewed|team reviewed"], [r"option B is"], [r"It costs|It will cost"],
            [r"€12,400"], [r"apiVersion"], [r"17 March"],
            [r"supplier has not confirmed|supplier hasn['’]t confirmed"],
            [r"https://example\.com/v2"],
        ],
        "forbid": [r"team have reviewed", r"option B are", r"supplier have not"],
    },
    {
        "id": "write_concise",
        "category": "writing",
        "mode": "More Concise",
        "language": "English",
        "text": (
            "At this point in time, the committee is currently considering whether or not to "
            "postpone the launch until a later date because two required safety certificates have "
            "not yet been received."
        ),
        "criteria": [
            [r"committee"], [r"consider|may|whether"], [r"postpon"], [r"launch"],
            [r"two|2"], [r"required safety certificates|safety certificates"],
            [r"not.*received|await|pending"],
        ],
        "format": "shorter_20",
    },
    {
        "id": "write_formal",
        "category": "writing",
        "mode": "More Formal (Academic)",
        "language": "English",
        "text": (
            "I reckon the pilot went pretty well: 37 of the 40 sensors stayed online, and the "
            "three failures all happened after temperatures fell below -10°C. We should probably "
            "run it again in January before buying 500 units."
        ),
        "criteria": [
            [r"37"], [r"40"], [r"three|3"], [r"below -?10\s*°?C|-10\s*°?C"],
            [r"January"], [r"500"], [r"repeat|conduct.*again|run.*again"],
            [r"before.*(purchas|buy|acquir)|prior to.*(purchas|buy|acquir)"],
        ],
        "forbid": [r"\bI reckon\b", r"pretty well", r"should probably"],
    },
    {
        "id": "write_simple",
        "category": "writing",
        "mode": "Simpler Language",
        "language": "English",
        "text": (
            "Because the licence is revocable at the provider's sole discretion, continued access "
            "cannot be regarded as an entitlement, notwithstanding the customer's prior expenditure "
            "on integration."
        ),
        "criteria": [
            [r"provider"], [r"revoke|cancel|take.*away|end"], [r"licen[cs]e|access"],
            [r"not guaranteed|no right|cannot assume|not entitled|does not.*right"],
            [r"customer|you"], [r"spent|paid|money|cost"], [r"integrat"],
        ],
        "forbid": [r"notwithstanding", r"regarded as an entitlement", r"sole discretion"],
    },
    {
        "id": "write_thesis",
        "category": "writing",
        "mode": "Sharpen the Thesis",
        "language": "English",
        "text": (
            "Last year, our support team spent 420 hours manually merging duplicate tickets. The "
            "proposed matching rule correctly identified 94% of duplicates in the trial and produced "
            "two false matches. For that reason, we should enable it with human review instead of "
            "leaving the current process unchanged."
        ),
        "criteria": [
            [r"420"], [r"94\s*%|94 percent"], [r"two false|2 false"],
            [r"human review|human oversight"], [r"enable|adopt|implement|use"],
            [r"duplicate ticket"],
        ],
        "format": "thesis_first",
    },
    {
        "id": "write_diplomatic",
        "category": "writing",
        "mode": "Diplomatically Worded",
        "language": "English",
        "text": (
            "Your report ignores the budget limits and the timeline is completely unrealistic. "
            "You need to redo the whole plan before Friday."
        ),
        "criteria": [
            [r"report|plan"], [r"budget"], [r"timeline|schedule"],
            [r"revis|redo|rework|update"], [r"Friday"],
        ],
        "forbid": [r"\bignores\b", r"completely unrealistic", r"you need to"],
    },
    {
        "id": "translate_bhs_en",
        "category": "translation",
        "mode": "Translate BHS → English",
        "language": "English",
        "text": (
            "Iako je ugovor potpisan u maju, isporuka kasni jer dobavljač još nije dobio izvoznu "
            "dozvolu. Ako dozvola ne stigne do petka, kupac može raskinuti ugovor bez naknade."
        ),
        "criteria": [
            [r"although|even though|despite"], [r"contract.*signed|signed.*contract"],
            [r"May"], [r"delivery.*delay|shipment.*delay|delivery is late"],
            [r"supplier|vendor"], [r"export (permit|licen[cs]e)"],
            [r"Friday"], [r"buyer|customer"], [r"terminat|cancel|withdraw"],
            [r"without (a )?(fee|charge|penalty|compensation)"],
        ],
        "forbid": [r"\bmaju\b", r"\bpetka\b", r"\bdobavlja"],
    },
    {
        "id": "translate_zh_en",
        "category": "translation",
        "mode": "Translate Chinese → English",
        "language": "English",
        "text": "尽管实验组的平均分提高了12%，研究人员仍警告说，样本只有48人，而且研究没有设置对照组，因此不能证明新方法导致了这一变化。",
        "criteria": [
            [r"although|despite|even though"], [r"experimental group|treatment group"],
            [r"average score|mean score"], [r"12\s*%|12 percent"],
            [r"researcher.*warn|warn.*researcher"], [r"48"], [r"sample"],
            [r"no control group|without a control group|lacked a control group"],
            [r"cannot|could not|does not"], [r"prove|establish|demonstrate"],
            [r"new method.*caus|caus.*change|responsible.*change"],
        ],
        "forbid": [r"[\u4e00-\u9fff]"],
    },
    {
        "id": "translate_en_bhs",
        "category": "translation",
        "mode": "Translate English → BHS",
        "language": "BHS",
        "text": (
            "The backup completed successfully, but the report was not emailed because the mail "
            "server was offline. Do not delete the original files until Ana confirms receipt."
        ),
        "criteria": [
            [r"sigurnosna kopija|backup"], [r"uspješno|uspešno"], [r"izvje[šs]taj|izve[šs]taj"],
            [r"nije.*(poslan|poslat)|nije.*e-?mail|nije.*dostavljen"],
            [r"server.*(po[šs]t|mail)|server e-po[šs]te"], [r"offline|van mreže|nedostupan"],
            [r"ne bri[šs]ite|nemojte brisati"], [r"originalne datoteke|izvorne datoteke|originalne fajlove"],
            [r"Ana"], [r"potvrdi.*prijem|potvrdi.*primitak|potvrdi.*da je primila"],
        ],
        "forbid": [r"\bThe backup\b", r"Do not delete"],
    },
    {
        "id": "translate_en_zh",
        "category": "translation",
        "mode": "Translate English → Chinese",
        "language": "Chinese",
        "text": (
            "The revised budget reduces travel spending by 15%, but it does not cut the training "
            "fund. Managers must submit their updated plans before noon on Tuesday."
        ),
        "criteria": [
            [r"预算"], [r"差旅|旅行"], [r"15%|15％"], [r"培训"], [r"不.*削减|没有.*削减|保留"],
            [r"经理|管理人员"], [r"提交"], [r"更新.*计划|修订.*计划"], [r"星期二|周二"], [r"中午"],
        ],
        "forbid": [r"revised budget", r"Managers must"],
    },
]


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def _wait_ready(base_url: str, process: subprocess.Popen, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"llama-server exited with code {process.returncode}")
        try:
            if _get_json(base_url + "/health").get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise TimeoutError("llama-server did not become healthy")


def _messages(task: dict) -> tuple[list[dict], float, float]:
    mode = task["mode"]
    base = _REPHRASE_PROMPTS[mode]
    if task["category"] == "reading":
        lang = _answer_language_instruction(task["language"])
        system = lang + base
        user = f"Text:\n{task['text']}\n\n{lang}"
        repeat_penalty = 1.1
    elif task["category"] == "translation":
        system = base
        user = f"Rewrite:\n{task['text']}"
        repeat_penalty = 1.3
    else:
        system = _language_instruction(task["language"]) + base
        user = f"Rewrite:\n{task['text']}"
        repeat_penalty = 1.3
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], 0.35, repeat_penalty


def _stream_chat(base_url: str, task: dict, max_tokens: int = 512) -> dict:
    messages, temperature, repeat_penalty = _messages(task)
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "repeat_penalty": repeat_penalty,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        base_url + "/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    first_at = None
    pieces: list[str] = []
    usage: dict = {}
    finish_reason = None
    with urllib.request.urlopen(req, timeout=180) as response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            if line[6:] == "[DONE]":
                break
            chunk = json.loads(line[6:])
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                if first_at is None:
                    first_at = time.perf_counter()
                pieces.append(piece)
            if choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]
    ended = time.perf_counter()
    raw_text = "".join(pieces)
    text = _clean_llm_output(_strip_reasoning(raw_text)).strip()
    completion_tokens = usage.get("completion_tokens")
    generation_seconds = ended - (first_at or started)
    tokens_per_second = (
        completion_tokens / generation_seconds
        if completion_tokens and generation_seconds > 0
        else None
    )
    return {
        "output": text,
        "raw_output": raw_text,
        "wall_seconds": round(ended - started, 4),
        "ttft_seconds": round((first_at or ended) - started, 4),
        "generation_seconds": round(generation_seconds, 4),
        "completion_tokens": completion_tokens,
        "prompt_tokens": usage.get("prompt_tokens"),
        "tokens_per_second": round(tokens_per_second, 2) if tokens_per_second else None,
        "finish_reason": finish_reason,
    }


def _sentences(text: str) -> int:
    return len([part for part in re.split(r"(?<=[.!?])(?:[\"'’”)]*)\s+", text.strip()) if part])


def _score(task: dict, output: str) -> dict:
    matched = []
    for alternatives in task.get("criteria", []):
        matched.append(any(re.search(pattern, output, re.I | re.S) for pattern in alternatives))
    forbidden_hits = [
        pattern for pattern in task.get("forbid", [])
        if re.search(pattern, output, re.I | re.S)
    ]
    content_score = 100.0 * sum(matched) / max(1, len(matched))
    format_ok = True
    fmt = task.get("format")
    if fmt == "sentences_3":
        format_ok = bool(output.strip()) and _sentences(output) <= 3
    elif fmt == "bullets_5":
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        format_ok = bool(lines) and len(lines) <= 5 and all(
            line.startswith(("•", "-", "*")) for line in lines
        )
    elif fmt == "shorter_20":
        source_words = len(task["text"].split())
        output_words = len(output.split())
        format_ok = output_words <= source_words * 0.8
    elif fmt == "thesis_first":
        first_sentence = re.split(r"(?<=[.!?])\s+", output.strip(), maxsplit=1)[0]
        format_ok = bool(re.search(r"enable|adopt|implement|use|matching rule", first_sentence, re.I))
    score = content_score
    if not format_ok:
        score -= 10
    score -= 10 * len(forbidden_hits)
    if not output.strip():
        score = 0
    return {
        "score": round(max(0.0, min(100.0, score)), 1),
        "criteria_matched": matched,
        "forbidden_hits": forbidden_hits,
        "format_ok": format_ok,
    }


def _server_args(server: Path, model: dict, port: int, managed_fit: bool = False) -> list[str]:
    if managed_fit:
        # Mirrors vedium.managed_server.start_managed_server exactly.
        return [
            str(server),
            "--model", str(model["path"]),
            "--host", "127.0.0.1",
            "--port", str(port),
            "--ctx-size", "8192",
            "--parallel", "1",
            "--gpu-layers", "auto",
            "--fit", "on",
            "--fit-target", "1536",
            "--jinja",
            "--no-webui",
        ]
    cache = model.get("cache", "q8_0")
    return [
        str(server),
        "--model", str(model["path"]),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--ctx-size", "8192",
        "--parallel", "1",
        "--threads", "6",
        "--batch-size", str(model.get("batch", 2048)),
        "--ubatch-size", str(model.get("ubatch", 512)),
        "--n-gpu-layers", "999",
        "--cache-type-k", cache,
        "--cache-type-v", cache,
        "--flash-attn", "on",
        "--jinja",
        "--no-webui",
        *model.get("extra", []),
    ]


def _summary(task_results: list[dict]) -> dict:
    categories = {}
    for category in ("reading", "writing", "translation"):
        selected = [r for r in task_results if r["category"] == category and not r.get("error")]
        categories[category] = round(
            sum(r["quality"]["score"] for r in selected) / len(selected), 1
        ) if selected else None
    valid = [r for r in task_results if not r.get("error")]
    speeds = [r["tokens_per_second"] for r in valid if r.get("tokens_per_second")]
    return {
        "quality_by_category": categories,
        "quality_overall": round(
            sum(r["quality"]["score"] for r in valid) / len(valid), 1
        ) if valid else None,
        "median_tokens_per_second": round(sorted(speeds)[len(speeds) // 2], 2) if speeds else None,
        "mean_ttft_seconds": round(sum(r["ttft_seconds"] for r in valid) / len(valid), 3) if valid else None,
        "total_task_seconds": round(sum(r["wall_seconds"] for r in valid), 2),
        "tasks_completed": len(valid),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=Path, default=DEFAULT_SERVER)
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--tasks", default="")
    parser.add_argument("--output", type=Path, default=OUT_DIR / "results.json")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--managed-fit", action="store_true")
    args = parser.parse_args()

    selected = [item.strip() for item in args.models.split(",") if item.strip()]
    selected_tasks = [item.strip() for item in args.tasks.split(",") if item.strip()]
    unknown = [item for item in selected if item not in MODELS]
    if unknown:
        raise SystemExit(f"Unknown model key(s): {', '.join(unknown)}")
    known_task_ids = {task["id"] for task in TASKS}
    unknown_tasks = [item for item in selected_tasks if item not in known_task_ids]
    if unknown_tasks:
        raise SystemExit(f"Unknown task id(s): {', '.join(unknown_tasks)}")
    active_tasks = [task for task in TASKS if not selected_tasks or task["id"] in selected_tasks]
    if not args.server.is_file():
        raise SystemExit(f"llama-server not found: {args.server}")
    for key in selected:
        if not MODELS[key]["path"].is_file():
            raise SystemExit(f"Model not found: {MODELS[key]['path']}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = {
        "benchmark_version": 1,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server": str(args.server),
        "server_version": "",
        "settings": {
            "ctx_size": 8192,
            "threads": 6,
            "temperature": 0.35,
            "thinking": False,
            "repeat_penalty_reading": 1.1,
            "repeat_penalty_writing_translation": 1.3,
            "managed_fit": args.managed_fit,
        },
        "models": [],
    }
    try:
        version = subprocess.run(
            [str(args.server), "--version"], cwd=str(args.server.parent),
            capture_output=True, text=True, timeout=20,
        )
        results["server_version"] = (version.stdout + version.stderr).strip()
    except Exception as exc:
        results["server_version"] = f"unknown: {exc}"

    for key in selected:
        model = MODELS[key]
        print(f"\n=== {key}: {model['name']} ===", flush=True)
        log_path = args.output.parent / f"{key}.server.log"
        record = {
            "key": key,
            "name": model["name"],
            "path": str(model["path"]),
            "size_gib": round(model["path"].stat().st_size / (1024 ** 3), 2),
            "server_args": [],
            "load_seconds": None,
            "tasks": [],
            "error": None,
        }
        results["models"].append(record)
        process = None
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            try:
                port = _pick_port()
                command = _server_args(args.server, model, port, args.managed_fit)
                record["server_args"] = command[1:]
                load_started = time.perf_counter()
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                process = subprocess.Popen(
                    command,
                    cwd=str(args.server.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
                base_url = f"http://127.0.0.1:{port}"
                _wait_ready(base_url, process)
                record["load_seconds"] = round(time.perf_counter() - load_started, 2)
                print(f"loaded in {record['load_seconds']:.2f}s", flush=True)

                # Warm up CUDA kernels and the embedded chat template.
                warmup = {
                    "id": "warmup", "category": "reading", "mode": "Define This",
                    "language": "English", "text": "test", "criteria": [],
                }
                _stream_chat(base_url, warmup, max_tokens=8)

                for repetition in range(max(1, args.repeat)):
                    for task in active_tasks:
                        task_record = {
                            "id": task["id"],
                            "category": task["category"],
                            "mode": task["mode"],
                            "repetition": repetition + 1,
                        }
                        try:
                            task_record.update(_stream_chat(base_url, task))
                            task_record["quality"] = _score(task, task_record["output"])
                            print(
                                f"{task['id']}: q={task_record['quality']['score']:5.1f} "
                                f"t={task_record['wall_seconds']:6.2f}s "
                                f"tg={task_record.get('tokens_per_second')}",
                                flush=True,
                            )
                        except Exception as exc:
                            task_record["error"] = f"{type(exc).__name__}: {exc}"
                            print(f"{task['id']}: ERROR {task_record['error']}", flush=True)
                        record["tasks"].append(task_record)
                        record["summary"] = _summary(record["tasks"])
                        args.output.write_text(
                            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                print(f"MODEL ERROR: {record['error']}", flush=True)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
        record["summary"] = _summary(record["tasks"])
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResults: {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
