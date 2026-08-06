import asyncio
from typing import Any

import httpx
import pytest

from app.metadata.RecordedSeriesCandidates import (
    SearchWikipediaCandidates,
)


def test_search_wikipedia_candidates_parses_media_wiki_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def Handler(request: httpx.Request) -> httpx.Response:
        assert 'ja.wikipedia.org' in str(request.url)
        return httpx.Response(
            200,
            json={
                'query': {
                    'pages': [
                        {
                            'pageid': 42,
                            'title': '候補選択テスト',
                            'extract': 'Wikipedia候補の本文',
                            'index': 1,
                        },
                        {
                            'pageid': 7,
                            'title': '別候補',
                            'extract': '別本文',
                            'index': 0,
                        },
                    ],
                },
            },
            request=request,
        )

    original_client = httpx.AsyncClient

    def Client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = httpx.MockTransport(Handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    candidates = asyncio.run(SearchWikipediaCandidates('候補選択テスト', limit=5))
    assert [(item['page_id'], item['title']) for item in candidates] == [
        (7, '別候補'),
        (42, '候補選択テスト'),
    ]
