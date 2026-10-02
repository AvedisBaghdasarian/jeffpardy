"""Unit tests for the OpenTDB trivia client."""

import httpx
import pytest

from jeffpardy import trivia
from jeffpardy.trivia import (
    FALLBACK_BOARD,
    complete_with_fallback,
    fetch_board_data,
    group_results,
)


def test_group_results_decodes_html_and_caps_per_category():
    results = [
        {"category": "Science", "question": "Fish &amp; Chips?", "correct_answer": "Yes &amp; no"},
        *[{"category": "Science", "question": f"q{i}", "correct_answer": f"a{i}"} for i in range(10)],
    ]
    flat = results
    grouped = group_results(flat)
    assert len(grouped["Science"]) == 5
    assert grouped["Science"][0] == ("Fish & Chips?", "Yes & no")


def test_complete_with_fallback_fills_missing_categories():
    partial = {"Only": [(f"q{i}", f"a{i}") for i in range(5)]}
    board = complete_with_fallback(partial)
    assert len(board) == 6
    assert len(board["Only"]) == 5
    for category, pairs in board.items():
        assert len(pairs) == 5


async def _mock_client(payload: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio()
async def test_fetch_board_data_uses_api_when_full():
    cats = [f"Cat{i}" for i in range(6)]
    results = [
        {"category": cat, "question": f"q{cat}{i}", "correct_answer": f"a{cat}{i}"}
        for cat in cats
        for i in range(5)
    ]
    client = await _mock_client({"results": results})
    board = await fetch_board_data(client=client)
    assert set(board) == set(cats)
    await client.aclose()


@pytest.mark.asyncio()
async def test_fetch_board_data_falls_back_on_network_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    board = await fetch_board_data(client=client)
    assert set(board) == set(FALLBACK_BOARD)
    await client.aclose()
    assert trivia.FALLBACK_BOARD  # import sanity
