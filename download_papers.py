"""Utility for downloading papers listed in ``papers.txt`` using Sci-Hub."""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, List

from scihub.scihub import CaptchaNeedException, SciHub


logger = logging.getLogger(__name__)


KEYWORD_PREFIXES = (
    "compounds:",
    "measurement:",
    "temperature:",
    "pressure:",
)


@dataclass
class DownloadResult:
    title: str
    matched_title: str | None
    ratio: float | None
    file_path: str | None
    error: str | None = None
    doi: str | None = None


def _looks_like_author_line(line: str) -> bool:
    """Heuristically determine whether *line* lists author names."""

    if any(line.lower().startswith(prefix) for prefix in KEYWORD_PREFIXES):
        return False

    if re.search(r"\d", line):
        return False

    # Most author lines include commas separating family names and initials.
    if "," not in line:
        return False

    # Require at least one initial (e.g. "A.").
    return bool(re.search(r"[A-Z]\.\s?", line))


def _extract_titles(lines: Iterable[str]) -> List[str]:
    """Extract paper titles from the raw text of ``papers.txt``."""

    titles: List[str] = []
    current_title: List[str] = []
    capturing = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if capturing:
            if re.search(r"\d{4}", line):
                if current_title:
                    titles.append(" ".join(current_title))
                    current_title = []
                capturing = False
                # Re-process this line: it may be an author line for the next entry.
                if _looks_like_author_line(line):
                    capturing = True
                continue
            current_title.append(line)
            continue

        if _looks_like_author_line(line):
            capturing = True
            current_title = []

    if capturing and current_title:
        titles.append(" ".join(current_title))

    return titles


def _sanitize_filename(title: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._-")
    return safe or "paper"


def _fuzzy_ratio(target: str, candidate: str) -> float:
    return SequenceMatcher(None, target.lower(), candidate.lower()).ratio()


def download_papers(
    papers_file: str = "papers.txt",
    output_dir: str = "papers",
    search_limit: int = 10,
    min_ratio: float = 0.6,
) -> List[DownloadResult]:
    """Download papers listed in *papers_file* into *output_dir*."""

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    with open(papers_file, "r", encoding="utf-8") as handle:
        titles = _extract_titles(handle.readlines())

    scihub = SciHub()
    results: List[DownloadResult] = []

    for title in titles:
        logger.info("Searching for: %s", title)
        try:
            search_results = scihub.search(title, limit=search_limit)
        except Exception as exc:  # pragma: no cover - network call
            results.append(
                DownloadResult(
                    title=title,
                    matched_title=None,
                    ratio=None,
                    file_path=None,
                    error=str(exc),
                )
            )
            continue

        papers = search_results.get("papers", [])
        if not papers:
            results.append(
                DownloadResult(title, None, None, None, "No search results returned")
            )
            continue

        ranked_candidates = []
        for paper in papers:
            paper_title = paper.get("name", "")
            ranked_candidates.append(
                (paper, _fuzzy_ratio(title, paper_title))
            )

        ranked_candidates.sort(key=lambda item: item[1], reverse=True)

        matched_paper = None
        matched_ratio = None
        matched_doi = None
        captcha_failure = False

        for index, (paper, ratio) in enumerate(ranked_candidates):
            if ratio < min_ratio:
                break

            identifier = paper.get("url", "")

            try:
                doi = None
                if index == 0:
                    doi, _ = scihub.search_for_doi(
                        paper.get("name", ""), limit=search_limit
                    )
                if not doi:
                    doi = scihub.resolve_identifier_to_doi(identifier)
            except CaptchaNeedException as exc:
                results.append(
                    DownloadResult(
                        title,
                        paper.get("name"),
                        ratio,
                        None,
                        str(exc),
                    )
                )
                captcha_failure = True
                break

            if doi:
                matched_paper = paper
                matched_ratio = ratio
                matched_doi = doi
                break

        if captcha_failure:
            continue

        if not matched_paper:
            best_candidate = ranked_candidates[0][0] if ranked_candidates else {}
            best_ratio = ranked_candidates[0][1] if ranked_candidates else None
            error_message = "No viable search candidates"
            if ranked_candidates:
                if best_ratio is not None and best_ratio >= min_ratio:
                    error_message = "Unable to resolve DOI from search results"
                elif best_ratio is not None:
                    error_message = f"Best match below threshold ({best_ratio:.2f} < {min_ratio})"

            results.append(
                DownloadResult(
                    title,
                    best_candidate.get("name") if best_candidate else None,
                    best_ratio,
                    None,
                    error_message,
                )
            )
            continue

        filename = _sanitize_filename(title) + ".pdf"
        try:
            data = scihub.download(
                matched_doi,
                destination=output_dir,
                path=filename,
            )
        except CaptchaNeedException as exc:
            results.append(
                DownloadResult(
                    title,
                    matched_paper.get("name"),
                    matched_ratio,
                    None,
                    str(exc),
                    matched_doi,
                )
            )
            continue

        if "err" in data:
            results.append(
                DownloadResult(
                    title,
                    matched_paper.get("name"),
                    matched_ratio,
                    None,
                    data["err"],
                    matched_doi,
                )
            )
            continue

        file_path = os.path.join(output_dir, filename)
        results.append(
            DownloadResult(
                title,
                matched_paper.get("name"),
                matched_ratio,
                file_path,
                doi=matched_doi,
            )
        )

    return results


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    results = download_papers()
    success = 0
    for result in results:
        if result.file_path:
            logger.info(
                "Downloaded '%s' to %s (matched '%s' with ratio %.2f)",
                result.title,
                result.file_path,
                result.matched_title,
                result.ratio,
            )
            success += 1
        else:
            logger.warning(
                "Failed to download '%s': %s", result.title, result.error or "Unknown error"
            )

    logger.info("Successfully downloaded %d/%d papers", success, len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

