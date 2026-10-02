"""Trivia question client for board content.

Fetches fresh questions from a public trivia API (free, no key required),
groups them by category, and hands ordered (question, answer) pairs to
``game.build_board`` which assigns values.

A small set of classic fallback clues keeps the game playable (and tests
deterministic) when the network is unavailable.
"""

from __future__ import annotations

import html
import logging
from collections import OrderedDict

import httpx

log = logging.getLogger(__name__)

API_URL = "https://opentdb.com/api.php"
CATEGORY_URL = "https://opentdb.com/api_category.php"
CLUES_PER_CATEGORY = 5
CATEGORIES_PER_BOARD = 6

FALLBACK_BOARD: dict[str, list[tuple[str, str]]] = {
    "Science": [
        ("The chemical symbol for water.", "H2O"),
        ("The planet known as the Red Planet.", "Mars"),
        ("The gas plants absorb from the atmosphere.", "Carbon dioxide"),
        ("The force that pulls objects toward Earth.", "Gravity"),
        ("The particle that carries a negative charge.", "Electron"),
    ],
    "History": [
        ("The year the Berlin Wall fell.", "1989"),
        ("The ancient pyramids are found in this country.", "Egypt"),
        ("This ship famously sank in 1912.", "Titanic"),
        ("The empire that built the Colosseum.", "Rome"),
        ("The wall built across northern England by Romans.", "Hadrian's Wall"),
    ],
    "Geography": [
        ("The largest ocean on Earth.", "Pacific"),
        ("The capital of France.", "Paris"),
        ("The desert covering much of northern Africa.", "Sahara"),
        ("The river that flows through Egypt.", "Nile"),
        ("The smallest continent by land area.", "Australia"),
    ],
    "Arts": [
        ("He painted the Mona Lisa.", "Leonardo da Vinci"),
        ("This composer wrote the Fifth Symphony.", "Beethoven"),
        ("The author of 'Romeo and Juliet'.", "Shakespeare"),
        ("Starry Night was painted by this artist.", "Van Gogh"),
        ("The novel that begins 'Call me Ishmael'.", "Moby-Dick"),
    ],
    "Sports": [
        ("The number of players on a soccer team.", "Eleven"),
        ("The Olympic rings count.", "Five"),
        ("Tennis score after winning the first point.", "Fifteen"),
        ("The city that hosted the 2012 Olympics.", "London"),
        ("A marathon is roughly this many miles.", "Twenty-six"),
    ],
    "Tech": [
        ("HTML stands for HyperText ___ Language.", "Markup"),
        ("The company that created the iPhone.", "Apple"),
        ("Python was named after this comedy group.", "Monty Python"),
        ("The protocol that powers the web.", "HTTP"),
        ("The data structure with LIFO ordering.", "Stack"),
    ],
}


def clean(text: str) -> str:
    return html.unescape(text).strip()


def group_results(results: list[dict], per_category: int = CLUES_PER_CATEGORY) -> dict[str, list[tuple[str, str]]]:
    """Group raw OpenTDB results into category -> [(question, answer)]."""
    grouped: dict[str, list[tuple[str, str]]] = OrderedDict()
    for item in results:
        category = clean(str(item.get("category", "General")))
        question = clean(str(item.get("question", "")))
        answer = clean(str(item.get("correct_answer", "")))
        if not question or not answer:
            continue
        grouped.setdefault(category, [])
        if len(grouped[category]) < per_category:
            grouped[category].append((question, answer))
    return grouped


def complete_with_fallback(
    grouped: dict[str, list[tuple[str, str]]],
    needed_categories: int = CATEGORIES_PER_BOARD,
    per_category: int = CLUES_PER_CATEGORY,
) -> dict[str, list[tuple[str, str]]]:
    """Top up partial API data with fallback clues so boards are always full."""
    complete: dict[str, list[tuple[str, str]]] = OrderedDict()
    for category, pairs in grouped.items():
        if len(complete) >= needed_categories:
            break
        if len(pairs) >= per_category:
            complete[category] = pairs[:per_category]
    for category, pairs in FALLBACK_BOARD.items():
        if len(complete) >= needed_categories:
            break
        if category not in complete:
            complete[category] = pairs[:per_category]
    return complete


async def fetch_board_data(
    amount: int = 36,
    client: httpx.AsyncClient | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """Fetch grouped board data, falling back to offline clues on any error."""
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=10) as owned:
                resp = await owned.get(API_URL, params={"amount": amount, "type": "multiple"})
        else:
            resp = await client.get(API_URL, params={"amount": amount, "type": "multiple"})
        resp.raise_for_status()
        results = resp.json().get("results", [])
        grouped = group_results(results)
        board = complete_with_fallback(grouped)
        if board:
            return board
    except Exception as exc:  # network or API failure -> offline fallback
        log.warning("OpenTDB fetch failed, using fallback board: %s", exc)
    return {k: list(v) for k, v in FALLBACK_BOARD.items()}


async def fetch_categories(client: httpx.AsyncClient | None = None) -> list[dict]:
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=10) as owned:
                resp = await owned.get(CATEGORY_URL)
        else:
            resp = await client.get(CATEGORY_URL)
        resp.raise_for_status()
        return resp.json().get("trivia_categories", [])
    except Exception as exc:
        log.warning("OpenTDB categories fetch failed: %s", exc)
        return [{"id": 0, "name": name} for name in FALLBACK_BOARD]


FINAL_FALLBACK = (
    "Internet Things",
    "This game's questions come from an API whose name is a word for a prize wheel.",
    "What is OpenTDB?",
)


async def fetch_final(client: httpx.AsyncClient | None = None) -> tuple[str, str, str]:
    """One random question for Ultimate Jeffpardy (offline fallback on error)."""
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=10) as owned:
                resp = await owned.get(API_URL, params={"amount": 1, "type": "multiple"})
        else:
            resp = await client.get(API_URL, params={"amount": 1, "type": "multiple"})
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            item = results[0]
            category = clean(str(item.get("category", "Final")))
            question = clean(str(item.get("question", "")))
            answer = clean(str(item.get("correct_answer", "")))
            if question and answer:
                return category, question, answer
    except Exception as exc:
        log.warning("OpenTDB final fetch failed, using fallback: %s", exc)
    return FINAL_FALLBACK


async def fetch_boards(
    use_fallback: bool = False,
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]], tuple[str, str, str]]:
    """Content bundle for a new game: Jeffpardy board, Double board, final clue.

    The two boards are fetched independently so Bonus Jeffpardy is a fresh
    deal. Any network failure falls back per-board (see ``fetch_board_data``).
    """
    if use_fallback:
        first = {k: list(v) for k, v in FALLBACK_BOARD.items()}
        second = {k: list(v) for k, v in FALLBACK_BOARD.items()}
        return first, second, FINAL_FALLBACK
    import asyncio

    async with httpx.AsyncClient(timeout=10) as client:
        first, second, final = await asyncio.gather(
            fetch_board_data(36, client),
            fetch_board_data(36, client),
            fetch_final(client),
        )
    return first, second, final
