"""
Analyze how much *clean* Darija we actually have in
`Lyte/fineweb-edu-darija-translated`.

Single end-to-end pass:

  Stage 1 - basic filters (length, char ratios, arabic fraction)
  Stage 2 - "bad" patterns (refusals, translator boilerplate, English echo,
            chat markers, copy-through, emails, repeated punctuation)
  Stage 3 - repetition heuristics (char run, top n-gram fraction,
            unique-token ratio)
  Stage 4 - exact dedup (sha1 over normalized darija)
  Stage 5 - near dedup + clustering via MinHash LSH on char 5-shingles
            (Jaccard ~ 0.7, 32 bands x 4 rows)

Writes to <output-dir>:
  - report.json        : full funnel + per-reason counts + cluster stats
  - report.md          : human-readable summary
  - rejects_sample.jsonl
  - clusters_sample.jsonl  (top near-dup clusters with examples)
  - kept_ids.txt       : src_idx of rows that survive every stage

Usage:
    python -m scripts.analyze_darija_quality --max-rows 200000
    python -m scripts.analyze_darija_quality --output-dir dev/clean_darija
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


HF_DATASET = "Lyte/fineweb-edu-darija-translated"
HF_SPLIT = "train"

ARABIC_RE = re.compile(
    r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
LATIN_RE = re.compile(r"[A-Za-z]")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")
REPEATED_PUNCT_RE = re.compile(r"([!?.,:;،؛])\1{4,}")
CHAT_MARKER_RE = re.compile(r"(?im)^\s*(?:system|user|assistant)\s*:")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
WORD_RE = re.compile(r"\S+")
# Char-run: 7+ identical chars. Educational content commonly uses long runs
# of `_`, `.`, `…`, `-`, `*`, `=` as fill-in-the-blank markers or separators —
# so the char-run check should ignore these. We only flag runs of LETTERS or
# DIGITS, which is what indicates true model-loop garbage.
CHAR_RUN_RE = re.compile(r"([^\W\d_])\1{6,}", re.UNICODE)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
# Western and Arabic-Indic digits 0-9
DIGIT_TRANSLATE = str.maketrans({
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
})
# Zero-width / formatting chars to strip
ZW_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
# Tatweel/kashida is decorative — strip for normalization
TATWEEL = "\u0640"
# Arabic letter unification (used for hashing only, not for kept text)
ARABIC_UNIFY = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي",
    "ؤ": "و",
    "ة": "ه",
})
# Combining diacritics (harakat / shadda / sukun)
HARAKAT_RE = re.compile(r"[\u064b-\u065f\u0670\u06d6-\u06ed]")

# Arabic refusal patterns — narrowly scoped to AI-self-reference / model
# apologies. Generic "ما يمكنش" is normal Darija negation, so we DO NOT match
# bare modals here. Must look like the assistant talking about itself.
ARABIC_REFUSAL_RE = re.compile(
    r"(أنا\s+(نموذج|ذكاء\s+اصطناعي|مساعد\s+ذكي)"
    r"|كنموذج\s+(لغوي|ذكاء)"
    r"|ما\s+(نقدرش|قدرتش)\s+(نعاونك|نجاوبك|نعطيك|نساعدك)"
    r"|آسف[،,]?\s+(ما|أنا)\s+(نقدرش|قدرتش|نموذج))"
)

REFUSAL_RE = re.compile(
    r"(?i)("
    r"as an ai|i cannot|i can't|i am unable|i'm sorry|sorry,?\s+i|"
    r"as a language model|i do not have|i don't have access"
    r")"
)
BOILERPLATE_RE = re.compile(
    r"(?im)^\s*("
    r"here(?:'s| is)\s+(?:the\s+)?(?:moroccan\s+)?darija"
    r"|translation\s*:"
    r"|darija(?:\s+translation)?\s*:"
    r"|الترجمة\s*:"
    r"|ها\s+(?:هي\s+)?الترجمة"
    r"|بالدارجة\s*:"
    r")"
)


# ----------------------------- config -----------------------------

@dataclass
class Cfg:
    min_en_chars: int = 32
    max_en_chars: int = 4_000
    min_darija_chars: int = 32
    max_darija_chars: int = 8_000
    min_arabic_chars: int = 16
    # Lowered from 0.55 after inspecting rejects: technical Darija with
    # inline English code/library names was being killed unfairly.
    min_arabic_fraction: float = 0.40
    min_length_ratio: float = 0.35
    max_length_ratio: float = 3.00
    # URL count: pretraining-friendly. Citation-heavy fineweb-edu articles
    # routinely have 5-10 URLs. Bumped 4 -> 12.
    max_url_count: int = 12
    # EN-side filters (mirror; usually only catches very malformed EN)
    # Bumped 8 -> 16; fineweb-edu academic content can cite a lot.
    en_max_url_count: int = 16
    en_min_alnum_fraction: float = 0.40  # reject EN that is mostly markup/punct
    # Number-set drift: catches *severe* mistranslated statistics / dates.
    # Default off because numbers are often legitimately paraphrased or
    # omitted in Darija (e.g. "1,000th" -> "ألف"). When enabled, we only
    # fire on dramatic drift (>= drift_min) when EN has many numbers.
    number_set_check: bool = False
    number_set_drift_min: int = 4
    number_set_en_min: int = 4
    # repetition
    # Raised from 0.20 -> 0.30: 0.20 was catching legitimate prose.
    max_top_4gram_fraction: float = 0.30
    # Top-3 4-gram fraction catches soft phrase loops where no single 4-gram
    # dominates but a few do (e.g. listicle bullets with rotating placeholders).
    max_top3_4gram_fraction: float = 0.45
    # Lowered from 0.35 -> 0.30: 0.35 caught some on-topic but legit Darija.
    min_unique_word_ratio: float = 0.30
    # dedup / lsh
    shingle_k: int = 5
    minhash_perms: int = 128
    lsh_bands: int = 32  # 32 * 4 = 128
    # output
    rejects_sample_per_reason: int = 5
    clusters_sample: int = 25


# ----------------------------- text utils -----------------------------

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", " ").replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # strip zero-width / RTL/LTR marks; they are invisible noise that breaks
    # exact-dedup hashing without changing the visible content.
    text = ZW_RE.sub("", text)
    text = "\n".join(SPACE_RE.sub(" ", line).strip()
                     for line in text.split("\n"))
    return BLANK_LINES_RE.sub("\n\n", text).strip()


def normalize_arabic_for_hash(text: str) -> str:
    """Aggressive Arabic-aware normalization for dedup hashing only.

    Strips tatweel, harakat, and unifies alef/ya/waw/teh-marbuta variants so
    that cosmetically-identical texts collide. Also unifies digits.
    """
    t = text.replace(TATWEEL, "")
    t = HARAKAT_RE.sub("", t)
    t = t.translate(ARABIC_UNIFY)
    t = t.translate(DIGIT_TRANSLATE)
    return t


def normalize_for_hash(text: str) -> str:
    # collapse whitespace, lowercase latin, unify arabic forms, unify digits
    t = normalize_arabic_for_hash(text)
    t = re.sub(r"\s+", " ", t).strip().casefold()
    return t


def number_set(text: str) -> set[str]:
    """Set of numbers appearing in text, with Arabic-Indic digits unified."""
    t = text.translate(DIGIT_TRANSLATE)
    return {m.group(0).replace(",", ".") for m in NUMBER_RE.finditer(t)}


# ----------------------------- repetition -----------------------------

def repetition_stats(text: str) -> tuple[float, float, int, float]:
    """Return (top_4gram_fraction, unique_word_ratio, char_run_max,
    top3_4gram_fraction). top3 catches soft phrase loops where no single
    4-gram dominates but a few do.
    """
    words = WORD_RE.findall(text)
    n = len(words)
    if n < 4:
        return 0.0, 1.0, 0, 0.0
    grams = [tuple(words[i:i + 4]) for i in range(n - 3)]
    c = Counter(grams)
    most = c.most_common(3)
    total = len(grams)
    top_frac = most[0][1] / total
    top3_frac = sum(cnt for _, cnt in most) / total
    uniq_ratio = len(set(words)) / n
    run = 0
    m = CHAR_RUN_RE.search(text)
    if m:
        run = len(m.group(0))
    return top_frac, uniq_ratio, run, top3_frac


# ----------------------------- minhash / lsh -----------------------------

class MinHasher:
    """Numpy-vectorized MinHash using a Mersenne-prime affine family."""

    def __init__(self, num_perms: int, seed: int = 1):
        import numpy as np
        rng = np.random.default_rng(seed)
        # use a 32-bit prime so a*x+b fits comfortably in uint64
        self.p = np.uint64((1 << 32) - 5)  # 4294967291, prime
        self.a = rng.integers(1, int(self.p), size=num_perms, dtype=np.uint64)
        self.b = rng.integers(0, int(self.p), size=num_perms, dtype=np.uint64)
        self.num_perms = num_perms
        self._np = np
        self._empty = tuple([0] * num_perms)

    def signature(self, shingles: Iterable[str]) -> tuple[int, ...]:
        np = self._np
        # Use a deterministic 32-bit hash (zlib.crc32) — Python's built-in
        # hash() is randomized per process via PYTHONHASHSEED, which means
        # the same shingle gets different values in different workers.
        # That silently breaks cross-process / cross-shard signature compare.
        import zlib
        xs = [zlib.crc32(s.encode("utf-8")) for s in shingles]
        if not xs:
            return self._empty
        x = np.asarray(xs, dtype=np.uint64)              # (S,)
        # broadcast: (P, 1) * (S,) -> (P, S)
        v = (self.a[:, None] * x[None, :] + self.b[:, None]) % self.p
        return tuple(int(m) for m in v.min(axis=1))


def char_shingles(text: str, k: int) -> set[str]:
    # use arabic-normalized form so near-dup catches cosmetic variants
    t = normalize_arabic_for_hash(text)
    t = re.sub(r"\s+", " ", t)
    if len(t) < k:
        return {t}
    return {t[i:i + k] for i in range(len(t) - k + 1)}


class UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        while self.parent.get(x, x) != x:
            self.parent[x] = self.parent.get(self.parent.get(x, x), x)
            x = self.parent[x]
        self.parent.setdefault(x, x)
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


# ----------------------------- main pipeline -----------------------------

def evaluate_row(en: str, darija: str, cfg: Cfg) -> tuple[str | None, str, int]:
    """Return (rejection_reason_or_None, cleaned_darija, num_emails_masked).

    We strip emails from the darija side rather than reject the row, because
    inspection of rejects showed the surrounding prose was almost always good.
    """
    if not en:
        return "missing_en", darija, 0
    if not darija:
        return "missing_darija", darija, 0

    # Mask emails in-place. Repeat strip to avoid leaving trailing artifacts.
    n_emails = len(EMAIL_RE.findall(darija))
    if n_emails:
        darija = EMAIL_RE.sub("<email>", darija)

    le, ld = len(en), len(darija)
    if le < cfg.min_en_chars:
        return "short_en", darija, n_emails
    if le > cfg.max_en_chars:
        return "long_en", darija, n_emails
    if ld < cfg.min_darija_chars:
        return "short_darija", darija, n_emails
    if ld > cfg.max_darija_chars:
        return "long_darija", darija, n_emails

    # ---- EN-side sanity (cheap, catches scraper garbage in source) ----
    # mostly-punctuation/markup EN suggests source itself is junk
    en_alnum = sum(1 for c in en if c.isalnum())
    if en_alnum / max(le, 1) < cfg.en_min_alnum_fraction:
        return "en_low_alnum", darija, n_emails
    if len(URL_RE.findall(en)) > cfg.en_max_url_count:
        return "en_too_many_urls", darija, n_emails

    arabic = len(ARABIC_RE.findall(darija))
    if arabic < cfg.min_arabic_chars:
        return "low_arabic_chars", darija, n_emails
    if arabic / max(ld, 1) < cfg.min_arabic_fraction:
        return "low_arabic_fraction", darija, n_emails

    ratio = ld / max(le, 1)
    if ratio < cfg.min_length_ratio:
        return "low_length_ratio", darija, n_emails
    if ratio > cfg.max_length_ratio:
        return "high_length_ratio", darija, n_emails

    if normalize_for_hash(en) == normalize_for_hash(darija):
        return "english_echo", darija, n_emails
    if REPEATED_PUNCT_RE.search(darija):
        return "repeated_punctuation", darija, n_emails
    if CHAT_MARKER_RE.search(darija):
        return "chat_marker", darija, n_emails
    if BOILERPLATE_RE.search(darija):
        return "translator_boilerplate", darija, n_emails
    if REFUSAL_RE.search(darija) or ARABIC_REFUSAL_RE.search(darija):
        return "refusal_text", darija, n_emails
    if len(URL_RE.findall(darija)) > cfg.max_url_count:
        return "too_many_urls", darija, n_emails

    # Number-set drift: catches severely mistranslated statistics.
    # Off by default; when on, only fires for dramatic, multi-number drift.
    if cfg.number_set_check:
        en_nums = number_set(en)
        if len(en_nums) >= cfg.number_set_en_min:
            d_nums = number_set(darija)
            drift = len(en_nums.symmetric_difference(d_nums))
            if drift >= cfg.number_set_drift_min:
                return "number_set_mismatch", darija, n_emails

    top_frac, uniq_ratio, run, top3_frac = repetition_stats(darija)
    if run >= 8:
        return "char_run_repetition", darija, n_emails
    if top_frac > cfg.max_top_4gram_fraction:
        return "ngram_repetition", darija, n_emails
    if top3_frac > cfg.max_top3_4gram_fraction:
        return "ngram_top3_repetition", darija, n_emails
    if uniq_ratio < cfg.min_unique_word_ratio:
        return "low_word_diversity", darija, n_emails

    return None, darija, n_emails


def iter_dataset(token: str | None, cache_dir: str | None,
                 max_rows: int | None, skip_rows: int = 0,
                 shard_files: list[str] | None = None):
    """Stream rows. Either:
      - default: stream the full dataset (uses .skip if skip_rows>0; note that
        HF IterableDataset.skip() can misbehave on very large offsets across
        many shards — prefer shard_files for reliable region sampling), or
      - shard_files: load only specific parquet shards (canonical way to
        sample different regions of the dataset cheaply).
    """
    from datasets import load_dataset
    if shard_files:
        ds = load_dataset(
            "parquet",
            data_files=shard_files,
            split="train",
            streaming=True,
            token=token,
            cache_dir=cache_dir,
        )
    else:
        ds = load_dataset(HF_DATASET, split=HF_SPLIT, streaming=True,
                          token=token, cache_dir=cache_dir)
        if skip_rows:
            ds = ds.skip(skip_rows)
    for i, row in enumerate(ds):
        if max_rows is not None and i >= max_rows:
            break
        yield skip_rows + i, row


def list_repo_shard_urls(repo_id: str, token: str | None) -> list[str]:
    """Return https URLs to the train parquet shards on the Hub, in order."""
    from huggingface_hub import HfApi
    api = HfApi(token=token)
    files = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
    parquet = sorted(f for f in files if f.endswith(".parquet"))
    if not parquet:
        raise RuntimeError(f"No parquet files found in {repo_id}")
    return [f"hf://datasets/{repo_id}/{p}" for p in parquet]


def process_window(
    cfg: Cfg,
    output_dir: Path,
    token: str | None,
    cache_dir: str | None,
    skip_rows: int,
    max_rows: int | None,
    window_label: str,
    shard_files: list[str] | None = None,
) -> dict:
    """Run the full filter+dedup+cluster pipeline on a single dataset window."""
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[analyze] dataset={HF_DATASET} split={HF_SPLIT} "
          f"window={window_label} skip={skip_rows} max={max_rows} "
          f"shards={len(shard_files) if shard_files else 0}")
    print(f"[analyze]   output_dir={output_dir}")

    reject_counts: Counter[str] = Counter()
    reject_samples: dict[str, list[dict]] = defaultdict(list)

    seen_total = 0
    emails_masked_total = 0
    rows_with_email = 0
    accepted_basic = 0
    accepted_chars = 0
    accepted_arabic_chars = 0

    exact_seen: dict[str, int] = {}
    exact_dups = 0

    survivors: list[tuple[int, str, str]] = []
    minhasher = MinHasher(cfg.minhash_perms, seed=1)
    sigs: list[tuple[int, ...]] = []

    t0 = time.time()
    for ord_i, row in iter_dataset(token, cache_dir, max_rows, skip_rows, shard_files):
        seen_total += 1
        en = normalize_text(row.get("en"))
        darija = normalize_text(row.get("darija"))

        reason, darija, n_masked = evaluate_row(en, darija, cfg)
        if n_masked:
            emails_masked_total += n_masked
            rows_with_email += 1
        if reason is not None:
            reject_counts[reason] += 1
            bucket = reject_samples[reason]
            if len(bucket) < cfg.rejects_sample_per_reason:
                bucket.append({
                    "src_idx": row.get("src_idx", ord_i),
                    "reason": reason,
                    "en": en[:400],
                    "darija": darija[:400],
                })
            continue

        accepted_basic += 1
        accepted_chars += len(darija)
        accepted_arabic_chars += len(ARABIC_RE.findall(darija))

        h = hashlib.sha1(normalize_for_hash(
            darija).encode("utf-8")).hexdigest()
        if h in exact_seen:
            exact_dups += 1
            continue
        exact_seen[h] = ord_i

        sig = minhasher.signature(char_shingles(darija, cfg.shingle_k))
        survivors.append((int(row.get("src_idx", ord_i)), darija, en, h))
        sigs.append(sig)

        if seen_total % 5000 == 0:
            elapsed = time.time() - t0
            rate = seen_total / max(elapsed, 1e-6)
            print(f"[analyze]   seen={seen_total} kept={len(survivors)} "
                  f"exact_dups={exact_dups} ({rate:.0f} rows/s)")

    print(f"[analyze]   streaming done: seen={seen_total} basic_kept={accepted_basic} "
          f"exact_dups={exact_dups} survivors={len(survivors)}")

    # LSH near-dup clustering
    bands = cfg.lsh_bands
    rows_per_band = cfg.minhash_perms // bands
    assert cfg.minhash_perms % bands == 0

    uf = UnionFind()
    for idx in range(len(survivors)):
        uf.parent.setdefault(idx, idx)

    for b in range(bands):
        buckets: dict[tuple, list[int]] = defaultdict(list)
        start = b * rows_per_band
        end = start + rows_per_band
        for idx, sig in enumerate(sigs):
            key = (b,) + sig[start:end]
            buckets[key].append(idx)
        for members in buckets.values():
            if len(members) > 1:
                first = members[0]
                for m in members[1:]:
                    uf.union(first, m)

    clusters: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(survivors)):
        clusters[uf.find(idx)].append(idx)

    cluster_sizes = sorted((len(v) for v in clusters.values()), reverse=True)
    near_dup_dropped = sum(s - 1 for s in cluster_sizes if s > 1)
    unique_clean = len(clusters)

    final_chars = 0
    final_arabic_chars = 0
    for members in clusters.values():
        rep = members[0]
        d = survivors[rep][1]
        final_chars += len(d)
        final_arabic_chars += len(ARABIC_RE.findall(d))

    top_clusters = sorted(clusters.values(), key=len, reverse=True)[
        :cfg.clusters_sample]
    cluster_records = []
    for members in top_clusters:
        if len(members) < 2:
            continue
        examples = []
        for m in members[:3]:
            sid, d, en, _h = survivors[m]
            examples.append(
                {"src_idx": sid, "en": en[:300], "darija": d[:300]})
        cluster_records.append({"size": len(members), "examples": examples})

    report = {
        "dataset": HF_DATASET,
        "split": HF_SPLIT,
        "window": {"label": window_label, "skip_rows": skip_rows, "max_rows": max_rows},
        "config": cfg.__dict__,
        "elapsed_seconds": round(time.time() - t0, 2),
        "funnel": {
            "rows_seen": seen_total,
            "passed_filters": accepted_basic,
            "exact_dup_dropped": exact_dups,
            "post_exact_dedup": len(survivors),
            "near_dup_dropped": near_dup_dropped,
            "unique_clean": unique_clean,
        },
        "fractions": {
            "passed_filters_pct": round(100 * accepted_basic / max(seen_total, 1), 2),
            "post_exact_dedup_pct": round(100 * len(survivors) / max(seen_total, 1), 2),
            "unique_clean_pct": round(100 * unique_clean / max(seen_total, 1), 2),
        },
        "chars": {
            "accepted_basic_total": accepted_chars,
            "accepted_basic_arabic": accepted_arabic_chars,
            "final_total": final_chars,
            "final_arabic": final_arabic_chars,
            "approx_tokens_final": round(final_chars / 3.5),
        },
        "emails": {
            "rows_with_email": rows_with_email,
            "total_emails_masked": emails_masked_total,
        },
        "rejects_by_reason": dict(reject_counts.most_common()),
        "cluster_size_histogram": Counter(cluster_sizes).most_common(20),
        "largest_cluster_sizes": cluster_sizes[:20],
    }

    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output_dir / "rejects_sample.jsonl").open("w", encoding="utf-8") as f:
        for reason, samples in reject_samples.items():
            for s in samples:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with (output_dir / "clusters_sample.jsonl").open("w", encoding="utf-8") as f:
        for c in cluster_records:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with (output_dir / "kept_ids.txt").open("w", encoding="utf-8") as f:
        for members in clusters.values():
            sid = survivors[members[0]][0]
            f.write(f"{sid}\n")
    # Cross-shard dedup needs (src_idx, exact_hash) for each cluster rep, so
    # persist it. One line per canonical kept row.
    with (output_dir / "survivors.jsonl").open("w", encoding="utf-8") as f:
        for members in clusters.values():
            sid, _d, _en, h = survivors[members[0]]
            f.write(json.dumps({"src_idx": sid, "exact_hash": h}) + "\n")
    # Also persist the MinHash signature for each canonical survivor, in row
    # order matching survivors.jsonl, so cross-shard near-dedup can rebuild
    # an LSH index without re-reading text. uint32 is enough since p < 2**32.
    import numpy as _np
    canonical_sig_rows = []
    for members in clusters.values():
        canonical_sig_rows.append(sigs[members[0]])
    if canonical_sig_rows:
        sig_arr = _np.asarray(canonical_sig_rows, dtype=_np.uint32)
    else:
        sig_arr = _np.zeros((0, cfg.minhash_perms), dtype=_np.uint32)
    _np.save(output_dir / "sigs.npy", sig_arr)
    (output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def aggregate_reports(reports: list[dict], output_dir: Path) -> dict:
    """Combine per-window reports into one summary."""
    agg = {
        "dataset": reports[0]["dataset"],
        "split": reports[0]["split"],
        "num_windows": len(reports),
        "windows": [r["window"] for r in reports],
        "totals": {
            "rows_seen": sum(r["funnel"]["rows_seen"] for r in reports),
            "passed_filters": sum(r["funnel"]["passed_filters"] for r in reports),
            "exact_dup_dropped": sum(r["funnel"]["exact_dup_dropped"] for r in reports),
            "post_exact_dedup": sum(r["funnel"]["post_exact_dedup"] for r in reports),
            "near_dup_dropped": sum(r["funnel"]["near_dup_dropped"] for r in reports),
            "unique_clean": sum(r["funnel"]["unique_clean"] for r in reports),
            "final_chars": sum(r["chars"]["final_total"] for r in reports),
            "final_arabic_chars": sum(r["chars"]["final_arabic"] for r in reports),
            "approx_tokens_final": sum(r["chars"]["approx_tokens_final"] for r in reports),
            "emails_masked": sum(r["emails"]["total_emails_masked"] for r in reports),
        },
        "per_window_pct": [
            {
                "label": r["window"]["label"],
                "skip_rows": r["window"]["skip_rows"],
                "rows_seen": r["funnel"]["rows_seen"],
                "passed_filters_pct": r["fractions"]["passed_filters_pct"],
                "unique_clean_pct": r["fractions"]["unique_clean_pct"],
            }
            for r in reports
        ],
        "rejects_by_reason": dict(
            sum((Counter(r["rejects_by_reason"])
                for r in reports), Counter()).most_common()
        ),
    }
    rs = agg["totals"]["rows_seen"]
    agg["totals_pct"] = {
        "passed_filters_pct": round(100 * agg["totals"]["passed_filters"] / max(rs, 1), 2),
        "post_exact_dedup_pct": round(100 * agg["totals"]["post_exact_dedup"] / max(rs, 1), 2),
        "unique_clean_pct": round(100 * agg["totals"]["unique_clean"] / max(rs, 1), 2),
    }
    (output_dir / "aggregate.json").write_text(
        json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "aggregate.md").write_text(render_aggregate_md(agg), encoding="utf-8")
    return agg


def render_aggregate_md(a: dict) -> str:
    lines = [f"# Multi-window Darija quality — {a['dataset']}", ""]
    lines.append(f"_{a['num_windows']} windows_")
    lines.append("")
    lines.append("## Per-window pass rates")
    lines.append("")
    lines.append(
        "| label | skip_rows | rows_seen | passed_filters_% | unique_clean_% |")
    lines.append("|---|---:|---:|---:|---:|")
    for w in a["per_window_pct"]:
        lines.append(
            f"| {w['label']} | {w['skip_rows']:,} | {w['rows_seen']:,} | "
            f"{w['passed_filters_pct']} | {w['unique_clean_pct']} |"
        )
    t = a["totals"]
    p = a["totals_pct"]
    lines.append("")
    lines.append("## Totals across all windows")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---:|")
    lines.append(f"| rows seen | {t['rows_seen']:,} |")
    lines.append(
        f"| passed filters | {t['passed_filters']:,} ({p['passed_filters_pct']}%) |")
    lines.append(
        f"| post exact-dedup | {t['post_exact_dedup']:,} ({p['post_exact_dedup_pct']}%) |")
    lines.append(
        f"| **unique clean** | **{t['unique_clean']:,} ({p['unique_clean_pct']}%)** |")
    lines.append(f"| near-dup dropped | {t['near_dup_dropped']:,} |")
    lines.append(f"| emails masked | {t['emails_masked']:,} |")
    lines.append(f"| final chars | {t['final_chars']:,} |")
    lines.append(
        f"| approx tokens (~chars/3.5) | {t['approx_tokens_final']:,} |")
    lines.append("")
    lines.append("## Aggregated reject reasons")
    lines.append("")
    lines.append("| reason | count |")
    lines.append("|---|---:|")
    for reason, n in a["rejects_by_reason"].items():
        lines.append(f"| {reason} | {n:,} |")
    return "\n".join(lines)


def _shard_worker(payload: dict) -> dict:
    """Top-level worker for ProcessPoolExecutor. Runs one shard end-to-end.

    Skips work if `report.json` already exists in the output subdir (resume).
    Returns the report dict.
    """
    shard_idx: int = payload["shard_idx"]
    shard_url: str = payload["shard_url"]
    sub_dir = Path(payload["sub_dir"])
    cfg = Cfg(**payload["cfg"])
    token = payload.get("token")
    cache_dir = payload.get("cache_dir")
    max_rows = payload.get("max_rows")

    report_path = sub_dir / "report.json"
    if report_path.exists():
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            pass  # corrupt -> recompute

    return process_window(
        cfg, sub_dir, token, cache_dir,
        skip_rows=0, max_rows=max_rows,
        window_label=f"shard{shard_idx:04d}",
        shard_files=[shard_url],
    )


def run_all_shards(
    cfg: Cfg,
    output_dir: Path,
    token: str | None,
    cache_dir: str | None,
    max_rows: int | None,
    workers: int,
    shard_subset: list[int] | None = None,
) -> int:
    shard_urls = list_repo_shard_urls(HF_DATASET, token)
    total_shards = len(shard_urls)
    print(f"[analyze] discovered {total_shards} parquet shards")

    indices = shard_subset if shard_subset is not None else list(
        range(total_shards))
    print(f"[analyze] processing {len(indices)} shards with {workers} workers")

    payloads = []
    for idx in indices:
        if not (0 <= idx < total_shards):
            raise SystemExit(
                f"shard index {idx} out of range [0,{total_shards})")
        sub = output_dir / f"shard_{idx:04d}"
        payloads.append({
            "shard_idx": idx,
            "shard_url": shard_urls[idx],
            "sub_dir": str(sub),
            "cfg": cfg.__dict__,
            "token": token,
            "cache_dir": cache_dir,
            "max_rows": max_rows,
        })

    t0 = time.time()
    reports: list[dict] = []
    done = 0
    if workers <= 1:
        for p in payloads:
            rep = _shard_worker(p)
            reports.append(rep)
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (len(payloads) - done)
            print(f"[analyze] {done}/{len(payloads)} done "
                  f"(elapsed {elapsed/60:.1f}m, eta {eta/60:.1f}m)")
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_shard_worker, p)
                                 : p["shard_idx"] for p in payloads}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    rep = fut.result()
                except Exception as e:
                    print(f"[analyze] shard {idx} FAILED: {e}")
                    continue
                reports.append(rep)
                done += 1
                elapsed = time.time() - t0
                eta = elapsed / done * (len(payloads) - done)
                print(f"[analyze] shard {idx:04d} done | {done}/{len(payloads)} "
                      f"(elapsed {elapsed/60:.1f}m, eta {eta/60:.1f}m)")

    if not reports:
        print("[analyze] no reports produced")
        return 1

    # sort reports by shard index (from window label) for stable output
    def _shard_key(r):
        try:
            return int(r["window"]["label"].replace("shard", ""))
        except Exception:
            return 0
    reports.sort(key=_shard_key)

    agg = aggregate_reports(reports, output_dir)

    # ---------- Cross-shard exact dedup ----------
    # Each shard wrote survivors.jsonl with (src_idx, exact_hash) for its
    # canonical kept rows, plus sigs.npy with the matching MinHash signatures.
    # We do exact dedup first (cheap, hash-based) and then near dedup over
    # whatever survives, using a global LSH index.
    print("[analyze] running cross-shard exact dedup ...")
    global_seen: dict[str, tuple[int, int]] = {}   # hash -> (src_idx, shard_idx)
    cross_dup = 0
    total_survivors = 0
    # Track which (shard, local_row_idx) survive exact dedup so we can
    # subset their sigs for the near-dedup index.
    survivors_after_exact: list[tuple[int, int, int, str]] = []  # (shard, local_row, src_idx, h)

    for rep in reports:
        try:
            shard_idx = int(rep["window"]["label"].replace("shard", ""))
        except Exception:
            shard_idx = -1
        sub = output_dir / f"shard_{shard_idx:04d}"
        sjp = sub / "survivors.jsonl"
        if not sjp.exists():
            continue
        with sjp.open(encoding="utf-8") as f:
            for local_i, line in enumerate(f):
                s = json.loads(line)
                total_survivors += 1
                h = s["exact_hash"]
                sid = int(s["src_idx"])
                if h in global_seen:
                    cross_dup += 1
                    continue
                global_seen[h] = (sid, shard_idx)
                survivors_after_exact.append((shard_idx, local_i, sid, h))

    global_after_exact = len(survivors_after_exact)
    cross_exact_pct = round(100 * cross_dup / max(total_survivors, 1), 3)
    print(f"[analyze] cross-shard exact: {total_survivors:,} -> "
          f"{global_after_exact:,} ({cross_dup:,} dups, {cross_exact_pct}%)")

    # ---------- Cross-shard near dedup (global LSH) ----------
    print("[analyze] running cross-shard near dedup (global LSH) ...")
    import numpy as _np
    cfg = Cfg()
    bands = cfg.lsh_bands
    rows_per_band = cfg.minhash_perms // bands

    # Load sigs per shard, keep only rows that survived exact dedup.
    # We index by global row id (0..global_after_exact-1).
    shard_sig_cache: dict[int, "_np.ndarray"] = {}
    global_sigs = _np.zeros((global_after_exact, cfg.minhash_perms),
                            dtype=_np.uint32)
    for gi, (sh, local_i, _sid, _h) in enumerate(survivors_after_exact):
        if sh not in shard_sig_cache:
            sig_path = output_dir / f"shard_{sh:04d}" / "sigs.npy"
            shard_sig_cache[sh] = _np.load(sig_path)
        global_sigs[gi] = shard_sig_cache[sh][local_i]
    # Free per-shard caches as we go (just clear after we're done)
    shard_sig_cache.clear()

    # Build LSH band buckets and union-find.
    uf = UnionFind()
    for i in range(global_after_exact):
        uf.parent.setdefault(i, i)

    for b in range(bands):
        start = b * rows_per_band
        end = start + rows_per_band
        # Hash band tuples; tuple of small ints -> fits in a python dict key.
        buckets: dict[tuple, int] = {}
        band_slice = global_sigs[:, start:end]
        for gi in range(global_after_exact):
            key = (b,) + tuple(int(x) for x in band_slice[gi])
            first = buckets.get(key)
            if first is None:
                buckets[key] = gi
            else:
                uf.union(first, gi)
        del buckets

    # Walk clusters: keep root, drop the rest.
    cluster_root: dict[int, int] = {}  # gi -> root_gi
    for i in range(global_after_exact):
        cluster_root[i] = uf.find(i)
    cluster_size = Counter(cluster_root.values())
    near_dup_cross = global_after_exact - len(cluster_size)
    near_dup_pct = round(100 * near_dup_cross / max(total_survivors, 1), 3)
    print(f"[analyze] cross-shard near: {global_after_exact:,} -> "
          f"{len(cluster_size):,} ({near_dup_cross:,} near-dup dropped, "
          f"{near_dup_pct}%)")

    # Write the final global kept set: one entry per cluster root.
    final_kept_path = output_dir / "global_kept.jsonl"
    seen_roots: set[int] = set()
    with final_kept_path.open("w", encoding="utf-8") as gout:
        for gi in range(global_after_exact):
            root = cluster_root[gi]
            if root in seen_roots:
                continue
            seen_roots.add(root)
            sh, _li, sid, h = survivors_after_exact[gi]
            gout.write(json.dumps({
                "shard": sh, "src_idx": sid, "exact_hash": h
            }) + "\n")

    global_unique = len(cluster_size)

    # Largest cross-shard near-dup clusters for sanity reporting.
    top_cross = cluster_size.most_common(20)
    cross_cluster_sizes = [n for _, n in top_cross]

    # Patch the aggregate file with global numbers.
    agg["global_dedup"] = {
        "per_shard_survivors": total_survivors,
        "cross_shard_exact_dup_dropped": cross_dup,
        "after_cross_exact": global_after_exact,
        "cross_shard_near_dup_dropped": near_dup_cross,
        "global_unique": global_unique,
        "cross_shard_exact_dup_pct": cross_exact_pct,
        "cross_shard_near_dup_pct": near_dup_pct,
        "largest_cross_shard_clusters": cross_cluster_sizes,
    }
    (output_dir / "aggregate.json").write_text(
        json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_extra = (
        "\n\n## Cross-shard dedup\n\n"
        "| metric | value |\n|---|---:|\n"
        f"| per-shard survivors (in) | {total_survivors:,} |\n"
        f"| cross-shard exact dups dropped | {cross_dup:,} ({cross_exact_pct}%) |\n"
        f"| after cross-shard exact | {global_after_exact:,} |\n"
        f"| cross-shard near dups dropped | {near_dup_cross:,} ({near_dup_pct}%) |\n"
        f"| **global unique** | **{global_unique:,}** |\n"
    )
    if cross_cluster_sizes and cross_cluster_sizes[0] > 1:
        md_extra += "\nTop cross-shard near-dup cluster sizes: " + \
            ", ".join(str(s) for s in cross_cluster_sizes) + "\n"
    agg_md_path = output_dir / "aggregate.md"
    agg_md_path.write_text(agg_md_path.read_text(encoding="utf-8") + md_extra,
                           encoding="utf-8")

    (output_dir / "DEDUP_SCOPE.txt").write_text(
        "Within-shard: exact + near (MinHash LSH ~0.42 Jaccard).\n"
        "Across-shard: exact + near (global MinHash LSH).\n",
        encoding="utf-8",
    )
    print("\n" + render_aggregate_md(agg) + md_extra)
    print(f"\n[analyze] total elapsed: {(time.time()-t0)/60:.1f} min")
    return 0


def run(args: argparse.Namespace) -> int:
    cfg = Cfg()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    load_dotenv(Path(".env"))
    token = (args.hf_token or os.environ.get("HF_TOKEN")
             or os.environ.get("HUGGINGFACE_HUB_TOKEN"))

    # Full-dataset, multi-worker mode (one shard per task).
    if args.all_shards:
        subset = None
        if args.shards:
            subset = [int(x) for x in args.shards.split(",") if x.strip()]
        return run_all_shards(
            cfg, output_dir, token, args.cache_dir,
            max_rows=args.max_rows,
            workers=args.workers,
            shard_subset=subset,
        )

    # Shard-based sampling is more reliable than .skip() for probing different
    # regions of the dataset. If --shards is given, use that.
    shard_urls: list[str] | None = None
    shard_offsets: list[int] | None = None
    if args.shards:
        shard_urls = list_repo_shard_urls(HF_DATASET, token)
        print(f"[analyze] discovered {len(shard_urls)} parquet shards")
        shard_offsets = [int(x) for x in args.shards.split(",") if x.strip()]
        for off in shard_offsets:
            if not (0 <= off < len(shard_urls)):
                raise SystemExit(f"--shards offset {off} out of range "
                                 f"[0, {len(shard_urls)})")

    if shard_offsets is not None:
        reports: list[dict] = []
        for i, sh in enumerate(shard_offsets):
            sub = output_dir / f"window_{i:02d}_shard{sh:04d}"
            rep = process_window(
                cfg, sub, token, args.cache_dir,
                skip_rows=0, max_rows=args.max_rows,
                window_label=f"w{i:02d}@shard{sh}",
                shard_files=[shard_urls[sh]],
            )
            reports.append(rep)
        agg = aggregate_reports(reports, output_dir)
        print("\n" + render_aggregate_md(agg))
        return 0

    # Otherwise fall back to .skip()-based windows (single or multi).
    if args.windows:
        offsets = [int(x) for x in args.windows.split(",") if x.strip()]
    else:
        offsets = [args.skip_rows]

    reports = []
    if len(offsets) == 1:
        rep = process_window(
            cfg, output_dir, token, args.cache_dir,
            skip_rows=offsets[0], max_rows=args.max_rows,
            window_label=f"w_{offsets[0]}",
        )
        reports.append(rep)
        print("\n" + render_markdown(rep))
    else:
        for i, off in enumerate(offsets):
            sub = output_dir / f"window_{i:02d}_skip{off}"
            rep = process_window(
                cfg, sub, token, args.cache_dir,
                skip_rows=off, max_rows=args.max_rows,
                window_label=f"w{i:02d}@{off}",
            )
            reports.append(rep)
        agg = aggregate_reports(reports, output_dir)
        print("\n" + render_aggregate_md(agg))
    return 0


def render_markdown(r: dict) -> str:
    f = r["funnel"]
    p = r["fractions"]
    c = r["chars"]
    lines = []
    lines.append(f"# Darija quality funnel — {r['dataset']}")
    lines.append("")
    lines.append(f"_elapsed: {r['elapsed_seconds']}s_")
    lines.append("")
    lines.append("## Funnel")
    lines.append("")
    lines.append("| stage | rows | % of seen |")
    lines.append("|---|---:|---:|")
    lines.append(f"| rows seen | {f['rows_seen']:,} | 100.00 |")
    lines.append(
        f"| passed filters | {f['passed_filters']:,} | {p['passed_filters_pct']} |")
    lines.append(
        f"| post exact-dedup | {f['post_exact_dedup']:,} | {p['post_exact_dedup_pct']} |")
    lines.append(
        f"| **unique clean (after near-dedup)** | **{f['unique_clean']:,}** | **{p['unique_clean_pct']}** |")
    lines.append("")
    lines.append(f"- exact dups dropped: {f['exact_dup_dropped']:,}")
    lines.append(f"- near dups dropped: {f['near_dup_dropped']:,}")
    lines.append("")
    lines.append("## Reject reasons")
    lines.append("")
    lines.append("| reason | count |")
    lines.append("|---|---:|")
    for reason, n in r["rejects_by_reason"].items():
        lines.append(f"| {reason} | {n:,} |")
    lines.append("")
    lines.append("## Final clean Darija volume")
    lines.append("")
    lines.append(f"- total chars: {c['final_total']:,}")
    lines.append(f"- arabic chars: {c['final_arabic']:,}")
    lines.append(f"- approx tokens (~chars/3.5): {c['approx_tokens_final']:,}")
    lines.append("")
    lines.append("## Largest near-dup clusters")
    lines.append("")
    lines.append(", ".join(str(s) for s in r["largest_cluster_sizes"]))
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default="dev/clean_darija")
    ap.add_argument("--max-rows", type=int, default=None,
                    help="Cap rows scanned per window (None = all).")
    ap.add_argument("--skip-rows", type=int, default=0,
                    help="Skip N rows before sampling (single-window mode).")
    ap.add_argument("--windows", type=str, default=None,
                    help="Comma-separated skip offsets to probe multiple regions. "
                         "Each window scans --max-rows rows. "
                         "Example: --windows 0,200000,400000,600000,800000 --max-rows 2000")
    ap.add_argument("--shards", type=str, default=None,
                    help="Comma-separated parquet shard indices to probe "
                         "(preferred over --windows for large skips, since "
                         "IterableDataset.skip() plateaus on multi-shard data). "
                         "Example: --shards 0,100,250,400,520 --max-rows 2000")
    ap.add_argument("--all-shards", action="store_true",
                    help="Process every parquet shard in the dataset, in parallel, "
                         "writing one report per shard plus an aggregate report. "
                         "Resumes automatically if a shard already has a report.json. "
                         "Combine with --shards to restrict to a subset.")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2),
                    help="Number of parallel worker processes for --all-shards.")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--hf-token", default=None)
    return ap.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
