#!/usr/bin/env python3
from __future__ import annotations
import argparse, concurrent.futures, datetime as dt, email.utils, html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OA_SEARCH_URL = "https://api.openalex.org/works"
# OpenAlex returns all fields by default; use select= for subset if needed
DEFAULT_CONFIG = Path("config/interests.json")
DEFAULT_PARTITION_CONFIG = Path("config/journal_partitions.json")
DEFAULT_MATCH_WORDS = Path("config/match_words.json")
DEFAULT_OUTPUT = Path("web/data/papers.json")
RETAINED_MATCH_LEVELS = {"high", "medium"}
DEFAULT_MAX_STORED_PAPERS = 800
DEFAULT_MAX_DATA_BYTES = 8 * 1024 * 1024
DEFAULT_RECENT_HISTORY_DAYS = 45
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

@dataclass(frozen=True)
class Topic:
    id: str; name: str; description: str; keywords: list[str]

def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)

def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

def json_size_bytes(data: dict[str, Any]) -> int:
    return len(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")) + 1

def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()

def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()

def parse_topics(config: dict[str, Any]) -> list[Topic]:
    topics = []
    for item in config.get("topics", []):
        topic_id = item.get("id") or slugify(item.get("name", "topic"))
        topics.append(Topic(id=topic_id, name=item["name"],
            description=item.get("description", ""),
            keywords=[str(k) for k in item.get("keywords", [])]))
    if not topics:
        raise ValueError("No topics found in configuration.")
    return topics

def github_request(url: str, token: str) -> Any:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "paper-daily-collector",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def extract_json_block(markdown: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", markdown, flags=re.S | re.I)
    if fenced:
        return json.loads(fenced.group(1))
    stripped = markdown.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    return None

def load_issue_config(default_config: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("GITHUB_TOKEN", "")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    title = os.getenv("CONFIG_ISSUE_TITLE", "Research Interests")
    if not token or not repository:
        return default_config
    query = urllib.parse.urlencode({"state": "open", "per_page": "30"})
    url = f"https://api.github.com/repos/{repository}/issues?{query}"
    try:
        issues = github_request(url, token)
    except Exception as exc:
        print(f"Warning: cannot read GitHub issues, using config file: {exc}", file=sys.stderr)
        return default_config
    for issue in issues:
        if "pull_request" in issue:
            continue
        if issue.get("title", "").strip().lower() == title.lower():
            body = issue.get("body") or ""
            try:
                issue_config = extract_json_block(body)
            except json.JSONDecodeError as exc:
                print(f"Warning: config issue JSON is invalid, using config file: {exc}", file=sys.stderr)
                return default_config
            if issue_config and issue_config.get("topics"):
                return issue_config
    return default_config

def s2_query_for_topic(topic: Topic) -> str:
    """Build search query for OpenAlex (space-separated keywords)."""
    return " ".join(topic.keywords[:8])
def load_partitions(partition_path: Path | None = None) -> dict[str, set[str]]:
    path = partition_path or DEFAULT_PARTITION_CONFIG
    if not path.exists():
        return {}
    data = load_json(path)
    result: dict[str, set[str]] = {}
    for partition, names in data.items():
        if isinstance(names, list):
            result[partition] = {
                re.sub(r"[^a-z0-9]", "", str(name).lower())
                for name in names if str(name).strip()
            }
    return result

def lookup_partition(journal: str, partitions: dict[str, set[str]]) -> str:
    if not journal or not partitions:
        return "其他"
    normalized = re.sub(r"[^a-z0-9]", "", journal.lower())
    for partition, names in partitions.items():
        for name in names:
            if name and name in normalized:
                return partition
    return "其他"

def api_retry_wait_seconds(exc: Exception, attempt: int) -> float:
    min_wait = float(os.getenv("API_RETRY_MIN_SECONDS", "10"))
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return max(min_wait, float(retry_after))
    base = float(os.getenv("API_RETRY_BASE_SECONDS", "10"))
    cap = float(os.getenv("API_RETRY_MAX_SECONDS", "120"))
    return max(min_wait, min(cap, base * (2 ** attempt)))

def is_retryable_api_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP_CODES
    return isinstance(exc, (TimeoutError, urllib.error.URLError, OSError))

def should_stop_fetches(exc: Exception) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in {429, 503}

def reconstruct_abstract(inverted_index: dict | None) -> str:
    """Reconstruct abstract text from OpenAlex inverted index."""
    if not inverted_index or not isinstance(inverted_index, dict):
        return ""
    word_positions = []
    for word, positions in inverted_index.items():
        if isinstance(positions, list):
            for pos in positions:
                word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


def fetch_openalex(
    topic: Topic,
    max_results: int,
    partitions: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Fetch papers from OpenAlex API (free, no key, 100k requests/day)."""
    query = s2_query_for_topic(topic)
    per_page = min(max_results, 200)
    retry_count = int(os.getenv("API_RETRIES", "4"))
    timeout_seconds = float(os.getenv("API_TIMEOUT_SECONDS", "90"))
    max_retry_sec = float(os.getenv("API_RETRY_MAX_SECONDS", "180"))
    papers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    cursor = "*"

    headers = {
        "User-Agent": "paper-daily-collector/1.0",
        "mailto": os.getenv("CONTACT_EMAIL", ""),
    }

    while len(papers) < max_results:
        params = {
            "search": query,
            "sort": "publication_date:desc",
            "per_page": str(per_page),
            "cursor": cursor,
        }
        url = f"{OA_SEARCH_URL}?{urllib.parse.urlencode(params)}"

        last_error = None
        for attempt in range(retry_count):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as exc:
                last_error = exc
                if not is_retryable_api_error(exc) or attempt == retry_count - 1:
                    raise
                wait = api_retry_wait_seconds(exc, attempt)
                print(f"API temp error for {topic.name}: retrying in {wait:.0f}s", flush=True)
                time.sleep(wait)
        else:
            raise RuntimeError(f"OpenAlex request failed: {last_error}")

        results = data.get("results") or []
        if not results:
            break

        for item in results:
            paper_id = item.get("id", "")
            if not paper_id or paper_id in seen_ids:
                continue
            seen_ids.add(paper_id)

            # Authors
            authors_list = item.get("authorships") or []
            authors = [
                (a.get("author") or {}).get("display_name", "")
                for a in authors_list
                if (a.get("author") or {}).get("display_name")
            ]

            # DOI
            doi = item.get("doi", "")

            # Journal from primary location
            primary = item.get("primary_location") or {}
            source = primary.get("source") or {}
            journal_name = source.get("display_name", "")

            # Publication date
            pub_date = item.get("publication_date") or ""

            # Abstract (reconstruct from inverted index)
            abstract = reconstruct_abstract(item.get("abstract_inverted_index"))

            # PDF / landing page
            oa = item.get("open_access") or {}
            pdf_url = oa.get("oa_url", "") if oa else ""
            landing_url = primary.get("landing_page_url", "")
            paper_url = f"https://doi.org/{doi}" if doi else landing_url

            # Year
            year = int(pub_date[:4]) if pub_date else 0

            papers.append({
                "id": paper_id.rsplit("/", 1)[-1] if "/" in paper_id else paper_id,
                "source": "IEEE",
                "title": normalize_space(item.get("title", "")),
                "authors": [a for a in authors if a],
                "summary": normalize_space(abstract),
                "published": f"{pub_date}T00:00:00Z" if pub_date else "",
                "updated": f"{pub_date}T00:00:00Z" if pub_date else "",
                "year": year,
                "paper_url": paper_url,
                "pdf_url": pdf_url,
                "doi": doi,
                "journal": journal_name,
                "partition": lookup_partition(journal_name, partitions),
                "categories": [],
                "seed_topic": topic.id,
            })

        meta = data.get("meta") or {}
        next_cursor = meta.get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(0.5)  # OpenAlex: generous rate limit, 0.5s is safe

    return papers

def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)

def paper_datetime(paper: dict[str, Any]) -> dt.datetime:
    for field in ("published", "updated", "last_seen_at", "first_seen_at"):
        parsed = parse_datetime(str(paper.get(field, "")))
        if parsed:
            return parsed
    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)

def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def collection_cutoff(existing_payload: dict[str, Any], now: dt.datetime,
                      days: int, incremental_since_last_run: bool) -> tuple[dt.datetime, str]:
    if incremental_since_last_run:
        previous_run = parse_datetime(
            str(existing_payload.get("generated_at_iso") or existing_payload.get("generated_at") or ""))
        if previous_run:
            return previous_run, "incremental"
    return now - dt.timedelta(days=max(0, days)), "lookback"

def load_match_words(path: Path | None = None) -> dict[str, list[str]]:
    """Load per-topic match words from config. Falls back to empty dict."""
    p = path or DEFAULT_MATCH_WORDS
    if not p.exists():
        return {}
    data = load_json(p)
    return data.get("topics", {})


def keyword_score(topic: Topic, paper: dict[str, Any],
                  match_words_map: dict[str, list[str]] | None = None) -> tuple[float, list[str]]:
    """Score: required_words MUST hit (>=1), match_words contribute to score."""
    haystack = f"{paper.get('title', '')} {paper.get('summary', '')}".lower()
    hits = []
    weighted = 0.0
    required_words: list[str] = []

    if match_words_map and topic.id in match_words_map:
        entry = match_words_map[topic.id]
        if isinstance(entry, dict):
            required_words = [w.lower() for w in entry.get("required_words", [])]
            match_tokens = [w.lower() for w in entry.get("match_words", [])]
        else:
            match_tokens = [w.lower() for w in (entry if isinstance(entry, list) else [])]
    else:
        match_tokens = [w.lower() for kw in topic.keywords for w in kw.split()]

    # Required words: at least one must match, otherwise score=0
    if required_words:
        if not any(rw in haystack for rw in required_words if rw and len(rw) >= 2):
            return 0.0, []

    for token in match_tokens:
        if not token or len(token) < 2:
            continue
        if " " in token:
            if token in haystack:
                hits.append(token)
                weighted += 1.0
        else:
            if token in haystack:
                hits.append(token)
                weighted += 0.3

    divisor = max(1.0, len(match_tokens) * 0.3)
    score = min(1.0, weighted / divisor)
    return score, list(dict.fromkeys(hits))[:8]
def lexical_overlap_score(topic: Topic, paper: dict[str, Any]) -> float:
    topic_terms = set(re.findall(r"[a-zA-Z0-9]+", f"{topic.description} {" ".join(topic.keywords)}".lower()))
    paper_terms = set(re.findall(r"[a-zA-Z0-9]+", f"{paper.get("title", "")} {paper.get("summary", "")}".lower()))
    if not topic_terms or not paper_terms:
        return 0.0
    overlap = topic_terms & paper_terms
    return min(1.0, len(overlap) / max(8, len(topic_terms) * 0.18))

def match_level(score: float) -> str:
    if score >= 0.72: return "high"
    if score >= 0.42: return "medium"
    return "low"

def score_paper(topic: Topic, paper: dict[str, Any],
                match_words_map: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Score paper against a topic. Returns 0 if required_words not matched."""
    # Required words gate: at least one must match
    if match_words_map and topic.id in match_words_map:
        entry = match_words_map[topic.id]
        if isinstance(entry, dict):
            required = [w.lower() for w in entry.get("required_words", [])]
            if required:
                haystack = f"{paper.get('title', '')} {paper.get('summary', '')}".lower()
                if not any(rw in haystack for rw in required if rw and len(rw) >= 2):
                    return {"topic_id": topic.id, "topic_name": topic.name,
                            "score": 0.0, "level": "low",
                            "reason": f"?????: {required}", "keyword_hits": []}

    k_score, hits = keyword_score(topic, paper, match_words_map)
    l_score = lexical_overlap_score(topic, paper)
    base_score = round(0.65 * k_score + 0.35 * l_score, 3)
    reason_parts = []
    if hits:
        reason_parts.append("??????" + "?".join(hits[:6]))
    if not reason_parts:
        reason_parts.append("??????????????????????")
    return {"topic_id": topic.id, "topic_name": topic.name, "score": base_score,
            "level": match_level(base_score), "reason": "?".join(reason_parts),
            "keyword_hits": hits[:6]}
def fallback_summary(paper: dict[str, Any], best_match: dict[str, Any]) -> dict[str, str]:
    abstract = paper.get("summary", "")
    first_sentence = re.split(r"(?<=[.!?])\s+", abstract)[0] if abstract else ""
    return {"problem": "未配置模型 API，当前仅基于标题、摘要和关键词生成基础摘要。",
            "method": first_sentence[:300] if first_sentence else "请打开论文链接查看方法细节。",
            "innovation": "需要接入模型 API 后自动抽取更精确的中文创新点。",
            "evidence": "来源摘要可在论文原文中核验。",
            "limitations": "基础模式不会阅读全文，也不会进行深度技术对比。",
            "why_relevant": best_match.get("reason", "与配置方向存在文本匹配。")}

def llm_enabled() -> bool:
    return bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY"))

def llm_headers(api_key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
            "User-Agent": "paper-daily-collector/1.0"}

def call_openai_compatible(prompt: str) -> dict[str, Any]:
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
    base_url = os.getenv("LLM_BASE_URL", "")
    if not base_url:
        base_url = "https://api.deepseek.com/v1" if os.getenv("DEEPSEEK_API_KEY") else "https://api.openai.com/v1"
    model = os.getenv("LLM_MODEL", "deepseek-chat" if os.getenv("DEEPSEEK_API_KEY") else "gpt-4o-mini")
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "temperature": 0.2,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": "你是严谨的论文技术分析助手。只输出合法 JSON，不要输出 Markdown。"},
                            {"role": "user", "content": prompt}]}
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                                 headers=llm_headers(api_key), method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return json.loads(data["choices"][0]["message"]["content"])

def build_llm_prompt(topic: Topic, paper: dict[str, Any], base_match: dict[str, Any]) -> str:
    journal_line = f"期刊：{paper.get("journal", "未知")}\n分区：{paper.get("partition", "其他")}" if paper.get("journal") else ""
    return f"""请根据论文标题、摘要和我的研究方向，输出精确中文分析。不要夸大摘要中没有的信息；如果证据不足，请明确说明。

我的研究方向：
名称：{topic.name}
描述：{topic.description}
关键词：{", ".join(topic.keywords)}

论文信息：
标题：{paper.get("title", "")}
作者：{", ".join(paper.get("authors", [])[:8])}
摘要：{paper.get("summary", "")}
{journal_line}

基础匹配信息：
分数：{base_match.get("score")}
等级：{base_match.get("level")}
原因：{base_match.get("reason")}

请输出 JSON，字段必须为：
{{
  "problem": "论文要解决的问题，中文，1-2句",
  "method": "核心方法，中文，1-2句",
  "innovation": "相对已有工作的具体创新点，中文，2-3点合并成一段",
  "evidence": "摘要中可核验的实验、理论或系统证据；没有则写证据不足",
  "limitations": "可能局限或需要阅读全文确认的点",
  "why_relevant": "为什么匹配我的研究方向",
  "match_score_adjustment": 0.0,
  "match_level": "high|medium|low"
}}"""

def summarize_with_llm(topic: Topic, paper: dict[str, Any],
                       base_match: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    if not llm_enabled():
        return fallback_summary(paper, base_match), base_match
    prompt = build_llm_prompt(topic, paper, base_match)
    try:
        data = call_openai_compatible(prompt)
    except Exception as exc:
        print(f"Warning: LLM summary failed for {paper.get("id")}: {exc}", file=sys.stderr)
        return fallback_summary(paper, base_match), base_match
    summary = {"problem": str(data.get("problem", "")),
               "method": str(data.get("method", "")),
               "innovation": str(data.get("innovation", "")),
               "evidence": str(data.get("evidence", "")),
               "limitations": str(data.get("limitations", "")),
               "why_relevant": str(data.get("why_relevant", ""))}
    adjustment = float(data.get("match_score_adjustment", 0.0) or 0.0)
    adjusted_score = max(0.0, min(1.0, base_match["score"] + adjustment))
    adjusted_level = str(data.get("match_level") or match_level(adjusted_score)).lower()
    if adjusted_level not in {"high", "medium", "low"}:
        adjusted_level = match_level(adjusted_score)
    adjusted_match = dict(base_match)
    adjusted_match["score"] = round(adjusted_score, 3)
    adjusted_match["level"] = adjusted_level
    adjusted_match["llm_reason"] = summary["why_relevant"]
    return summary, adjusted_match

def summarize_one(args: tuple[Topic, dict[str, Any]]) -> tuple[str, dict[str, str], dict[str, Any]]:
    topic, paper = args
    paper_id = str(paper.get("id", ""))
    summary, adjusted_match = summarize_with_llm(topic, paper, paper["best_match"])
    return paper_id, summary, adjusted_match

def dedupe_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by paper ID, then by DOI to catch cross-source duplicates."""
    seen_ids = set()
    seen_dois = set()
    unique = []
    for paper in papers:
        key = paper.get("id") or paper.get("paper_url")
        doi = (paper.get("doi") or "").lower()
        if key in seen_ids or (doi and doi in seen_dois):
            continue
        seen_ids.add(key)
        if doi:
            seen_dois.add(doi)
        unique.append(paper)
    return unique
def paper_key(paper: dict[str, Any]) -> str:
    return str(paper.get("id") or paper.get("paper_url") or "")

def best_match_level(paper: dict[str, Any]) -> str:
    return str((paper.get("best_match") or {}).get("level") or "low").lower()

def load_existing_payload(output_path: Path) -> dict[str, Any]:
    if not output_path.exists():
        return {}
    try:
        return load_json(output_path)
    except Exception as exc:
        print(f"Warning: cannot read existing paper data, starting fresh: {exc}", file=sys.stderr)
        return {}

def merge_with_retained_papers(current_papers: list[dict[str, Any]],
                               existing_payload: dict[str, Any], now: dt.datetime,
                               recent_history_days: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    existing_papers = existing_payload.get("papers", []) if isinstance(existing_payload, dict) else []
    existing_generated_at = str(existing_payload.get("generated_at_iso") or
                                existing_payload.get("generated_at") or now.isoformat())
    retained_by_key: dict[str, dict[str, Any]] = {}
    dropped_low = 0
    retained_recent = 0
    for paper in existing_papers:
        if not isinstance(paper, dict):
            continue
        key = paper_key(paper)
        if not key:
            continue
        seen_at = parse_datetime(str(paper.get("first_seen_at") or
                                     paper.get("last_seen_at") or existing_generated_at))
        is_recent = bool(recent_history_days > 0 and seen_at and
                         (now.date() - seen_at.date()).days <= recent_history_days)
        if best_match_level(paper) in RETAINED_MATCH_LEVELS or is_recent:
            retained_by_key[key] = paper
            if is_recent and best_match_level(paper) not in RETAINED_MATCH_LEVELS:
                retained_recent += 1
        else:
            dropped_low += 1
    merged = []
    seen = set()
    now_iso = now.isoformat()
    for paper in current_papers:
        key = paper_key(paper)
        previous = retained_by_key.get(key)
        if previous:
            paper.setdefault("first_seen_at", previous.get("first_seen_at") or existing_generated_at)
        else:
            paper.setdefault("first_seen_at", now_iso)
        paper["last_seen_at"] = now_iso
        paper["retained_from_previous_run"] = False
        merged.append(paper)
        if key:
            seen.add(key)
    retained_count = 0
    for key, paper in retained_by_key.items():
        if key in seen:
            continue
        retained = dict(paper)
        retained.setdefault("first_seen_at", existing_generated_at)
        retained.setdefault("last_seen_at", existing_generated_at)
        retained["retained_from_previous_run"] = True
        merged.append(retained)
        retained_count += 1
    return dedupe_papers(merged), {"retained_paper_count": retained_count,
                                   "retained_recent_low_count": retained_recent,
                                   "dropped_low_relevance_count": dropped_low}

def deletion_sort_key(paper: dict[str, Any]) -> tuple[int, dt.datetime]:
    level = best_match_level(paper)
    return (0 if level == "low" else 1), paper_datetime(paper)

def trim_papers_for_storage(payload: dict[str, Any], max_stored_papers: int,
                            max_data_bytes: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    papers = list(payload.get("papers", []))
    removed_by_level = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    def projected_size() -> int:
        projected = dict(payload)
        projected["papers"] = papers
        return json_size_bytes(projected)
    data_bytes = projected_size()
    while papers and ((max_stored_papers > 0 and len(papers) > max_stored_papers) or
                      (max_data_bytes > 0 and data_bytes > max_data_bytes)):
        remove_index = min(range(len(papers)), key=lambda i: deletion_sort_key(papers[i]))
        removed = papers.pop(remove_index)
        level = best_match_level(removed)
        removed_by_level[level if level in removed_by_level else "unknown"] += 1
        data_bytes = projected_size()
    return papers, {"max_stored_papers": max_stored_papers, "max_data_bytes": max_data_bytes,
                    "data_bytes": data_bytes,
                    "storage_trimmed_count": sum(removed_by_level.values()),
                    "storage_trimmed_by_level": removed_by_level}

def collect(config_path: Path, output_path: Path, days: int, max_per_topic: int,
            max_summaries: int, max_stored_papers: int, max_data_bytes: int,
            incremental_since_last_run: bool, recent_history_days: int) -> dict[str, Any]:
    default_config = load_json(config_path)
    config = load_issue_config(default_config)
    topics = parse_topics(config)
    partitions = load_partitions()
    match_words_map = load_match_words()
    now = dt.datetime.now(dt.timezone.utc)
    existing_payload = load_existing_payload(output_path)
    cutoff, collection_mode = collection_cutoff(existing_payload, now, days, incremental_since_last_run)
    all_candidates = []
    successful_fetches = 0
    failed_fetches = 0
    for index, topic in enumerate(topics):
        if index:
            time.sleep(float(os.getenv("FETCH_DELAY_SECONDS", "5")))
        print(f"Fetching papers for topic: {topic.name}", flush=True)
        try:
            topic_papers = fetch_openalex(topic, max_per_topic, partitions)
            all_candidates.extend(topic_papers)
            successful_fetches += 1
        except Exception as exc:
            failed_fetches += 1
            print(f"Warning: fetch failed for {topic.name}: {exc}", file=sys.stderr)
            if should_stop_fetches(exc):
                skipped = len(topics) - index - 1
                failed_fetches += skipped
                if skipped:
                    print(f"Stopping fetches after {exc}; skipped {skipped} remaining topic(s).", file=sys.stderr)
                break

    if failed_fetches == len(topics) and existing_payload:
        existing = existing_payload
        if existing.get("papers"):
            print("All sources failed; preserving existing paper data.", file=sys.stderr)
            retained_papers, retention_stats = merge_with_retained_papers(
                [], existing_payload, now, recent_history_days)
            retained_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
            existing["papers"] = retained_papers
            existing["generated_at"] = email.utils.format_datetime(now)
            existing["generated_at_iso"] = now.isoformat()
            existing_stats = existing.setdefault("stats", {})
            existing_stats.update({"last_error": "All data source requests failed.",
                                   "successful_fetches": successful_fetches,
                                   "failed_fetches": failed_fetches, **retention_stats})
            trimmed, sstats = trim_papers_for_storage(existing, max_stored_papers, max_data_bytes)
            trimmed.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
            existing["papers"] = trimmed
            existing_stats.update(sstats)
            existing_stats["paper_count"] = len(trimmed)
            existing_stats["data_bytes"] = json_size_bytes(existing)
            write_json(output_path, existing)
            return existing

    recent_papers = []
    for paper in dedupe_papers(all_candidates):
        published = paper.get("published") or paper.get("updated")
        if not published:
            continue
        published_at = parse_datetime(str(published))
        if published_at and published_at >= cutoff:
            matches = [score_paper(topic, paper, match_words_map) for topic in topics]
            matches.sort(key=lambda item: item["score"], reverse=True)
            best_match = matches[0]
            if best_match["score"] <= 0:
                continue
            paper["matches"] = matches
            paper["best_match"] = best_match
            recent_papers.append(paper)

    recent_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
    summaries_by_id: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
    llm_jobs = []
    for paper in recent_papers[:max_summaries]:
        best_topic = next(t for t in topics if t.id == paper["best_match"]["topic_id"])
        llm_jobs.append((best_topic, paper))

    if llm_enabled() and llm_jobs:
        concurrency = max(1, int(os.getenv("LLM_CONCURRENCY", "2")))
        print(f"Summarizing {len(llm_jobs)} papers with LLM using concurrency={concurrency}", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(summarize_one, job) for job in llm_jobs]
            for future in concurrent.futures.as_completed(futures):
                pid, summary, adj = future.result()
                summaries_by_id[pid] = (summary, adj)
                print(f"Finished summary: {pid}", flush=True)
    else:
        for topic, paper in llm_jobs:
            summary, adj = summarize_with_llm(topic, paper, paper["best_match"])
            summaries_by_id[str(paper.get("id", ""))] = (summary, adj)

    for idx, paper in enumerate(recent_papers):
        pid = str(paper.get("id", ""))
        if idx < max_summaries and pid in summaries_by_id:
            summary, adj = summaries_by_id[pid]
            paper["chinese_summary"] = summary
            paper["best_match"] = adj
            paper["matches"] = [adj if m["topic_id"] == adj["topic_id"] else m for m in paper["matches"]]
        else:
            paper["chinese_summary"] = fallback_summary(paper, paper["best_match"])

    merged, retention_stats = merge_with_retained_papers(recent_papers, existing_payload, now, recent_history_days)
    merged.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)

    payload = {"generated_at": email.utils.format_datetime(now),
               "generated_at_iso": now.isoformat(),
               "config_source": "issue" if config is not default_config else "file",
               "topics": [t.__dict__ for t in topics], "papers": merged,
               "stats": {"paper_count": len(merged), "new_paper_count": len(recent_papers),
                         "days": days, "collection_mode": collection_mode,
                         "collection_cutoff_iso": cutoff.isoformat(),
                         "max_per_topic": max_per_topic, "llm_enabled": llm_enabled(),
                         "llm_concurrency": int(os.getenv("LLM_CONCURRENCY", "2")),
                         "recent_history_days": recent_history_days,
                         "successful_fetches": successful_fetches,
                         "failed_fetches": failed_fetches, **retention_stats}}
    trimmed, sstats = trim_papers_for_storage(payload, max_stored_papers, max_data_bytes)
    trimmed.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
    payload["papers"] = trimmed
    payload["stats"].update(sstats)
    payload["stats"]["paper_count"] = len(trimmed)
    payload["stats"]["data_bytes"] = json_size_bytes(payload)
    write_json(output_path, payload)
    return payload

def main() -> None:
    parser = argparse.ArgumentParser(description="Collect IEEE papers via Semantic Scholar for paper-daily.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "30")))
    parser.add_argument("--max-per-topic", type=int, default=int(os.getenv("MAX_PER_TOPIC", "25")))
    parser.add_argument("--max-summaries", type=int, default=int(os.getenv("MAX_SUMMARIES", "40")))
    parser.add_argument("--max-stored-papers", type=int, default=int(os.getenv("MAX_STORED_PAPERS", str(DEFAULT_MAX_STORED_PAPERS))))
    parser.add_argument("--max-data-bytes", type=int, default=int(os.getenv("MAX_DATA_BYTES", str(DEFAULT_MAX_DATA_BYTES))))
    parser.add_argument("--incremental-since-last-run", action="store_true", default=env_flag("INCREMENTAL_SINCE_LAST_RUN"))
    parser.add_argument("--recent-history-days", type=int, default=int(os.getenv("RECENT_HISTORY_DAYS", str(DEFAULT_RECENT_HISTORY_DAYS))))
    args = parser.parse_args()
    payload = collect(args.config, args.output, args.days, args.max_per_topic,
                      args.max_summaries, args.max_stored_papers, args.max_data_bytes,
                      args.incremental_since_last_run, args.recent_history_days)
    print(f"Wrote {len(payload["papers"])} papers to {args.output}")

if __name__ == "__main__":
    main()
