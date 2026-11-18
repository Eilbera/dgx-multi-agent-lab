#!/usr/bin/env python3
"""Discover AI YouTube channels from diverse story-first searches via yt-dlp."""
from __future__ import annotations

import concurrent.futures
import json
import subprocess
from collections import defaultdict
from pathlib import Path

QUERIES = [
    "machine learning research seminar", "artificial intelligence research talk", "deep learning research seminar",
    "large language model research talk", "LLM research paper explanation", "foundation models research seminar",
    "AI agents research talk", "agentic AI engineering", "AI alignment research seminar", "AI safety research talk",
    "mechanistic interpretability research", "LLM evaluation benchmark research", "AI red teaming technical talk",
    "reinforcement learning research seminar", "reasoning models research talk", "multimodal AI research talk",
    "natural language processing conference talk", "computer vision research seminar", "robotics AI research seminar",
    "speech recognition research talk", "audio generation AI research", "diffusion models research talk",
    "generative video model research", "world models machine learning research", "causal machine learning seminar",
    "scientific machine learning seminar", "AI for biology research talk", "AI for medicine research seminar",
    "graph neural networks research talk", "recommendation systems machine learning conference",
    "time series machine learning research", "geospatial AI research", "privacy preserving machine learning",
    "federated learning research seminar", "responsible AI research seminar", "AI governance technical research",
    "open source LLM engineering", "open source AI model release", "LLM inference engineering talk",
    "vLLM inference optimization", "llama.cpp technical talk", "GPU AI systems engineering",
    "distributed training large language models", "Mixture of Experts inference research", "LLM quantization research talk",
    "MLOps engineering conference", "machine learning systems conference", "data engineering for AI conference",
    "retrieval augmented generation technical talk", "vector database AI engineering", "AI observability evaluation engineering",
    "AI coding agents engineering", "computer use agents technical", "AI security conference talk",
    "NeurIPS talk machine learning", "ICML talk machine learning", "ICLR talk deep learning", "ACL NLP conference talk",
    "CVPR computer vision talk", "EMNLP NLP research talk", "MLSys machine learning systems talk", "COLM language modeling talk",
    "AAAI artificial intelligence conference", "AISTATS machine learning talk", "KDD machine learning research talk",
    "CoRL robot learning talk", "RSS robotics conference talk", "Interspeech speech AI talk", "ICASSP machine learning talk",
    "university AI institute seminar", "AI lab research channel", "machine learning summer school lecture",
    "deep learning course university", "machine learning paper reading group", "AI researcher podcast technical",
    "AI founder engineer interview technical", "language model researcher interview", "machine learning practitioner podcast",
    "aprendizaje automático investigación seminario", "inteligencia artificial investigación conferencia",
    "inteligência artificial pesquisa aprendizado de máquina", "apprentissage automatique recherche séminaire",
    "maschinelles lernen forschung vortrag", "intelligenza artificiale ricerca machine learning",
    "искусственный интеллект машинное обучение исследования", "الذكاء الاصطناعي تعلم الآلة أبحاث",
    "人工智能 机器学习 研究 讲座", "深度学习 大模型 研究", "機械学習 AI 研究 講演",
    "인공지능 머신러닝 연구 세미나", "मशीन लर्निंग आर्टिफिशियल इंटेलिजेंस रिसर्च",
    "kecerdasan buatan machine learning penelitian", "trí tuệ nhân tạo học máy nghiên cứu",
    "yapay zeka makine öğrenmesi araştırma", "uczenie maszynowe sztuczna inteligencja badania",
    "machine learning Africa research", "AI research Latin America", "AI research Middle East seminar",
    "AI research India seminar", "AI research Southeast Asia", "AI research China university",
]


def search(query: str, n: int = 25) -> tuple[str, list[dict], str | None]:
    cmd = ["yt-dlp", f"ytsearch{n}:{query}", "--flat-playlist", "--dump-single-json", "--quiet", "--no-warnings"]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=120, check=True)
        payload = json.loads(proc.stdout)
        return query, payload.get("entries") or [], None
    except Exception as exc:
        return query, [], f"{type(exc).__name__}: {exc}"


def main() -> int:
    out = Path("/home/USER/.hermes/profiles/youtube-ai-researcher/research/candidates_search.json")
    by_id: dict[str, dict] = {}
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for query, entries, error in pool.map(search, QUERIES):
            if error:
                errors.append({"query": query, "error": error})
                continue
            for entry in entries:
                channel_id = entry.get("channel_id")
                if not channel_id:
                    continue
                row = by_id.setdefault(channel_id, {
                    "name": entry.get("channel") or entry.get("uploader") or "",
                    "handle": (entry.get("uploader_id") or "").lstrip("@"),
                    "channel_id": channel_id,
                    "channel_url": entry.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}",
                    "category": "search-discovered",
                    "language_or_region": "unknown",
                    "kind": "unclassified",
                    "official_status": "unverified",
                    "feed_verified": False,
                    "verification_notes": "Discovered through YouTube search; requires quality and identity review.",
                    "discovery_url": entry.get("url") or "",
                    "sample_titles": [],
                    "query_hits": [],
                })
                if entry.get("title") and len(row["sample_titles"]) < 5:
                    row["sample_titles"].append(entry["title"])
                if query not in row["query_hits"]:
                    row["query_hits"].append(query)
    payload = {
        "queries": len(QUERIES),
        "candidate_count": len(by_id),
        "errors": errors,
        "candidates": sorted(by_id.values(), key=lambda r: (-len(r["query_hits"]), r["name"].lower())),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"path": str(out), "queries": len(QUERIES), "candidates": len(by_id), "errors": len(errors)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
