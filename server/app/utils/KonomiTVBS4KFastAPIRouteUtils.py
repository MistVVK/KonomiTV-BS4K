from collections.abc import Iterator, Sequence

from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from starlette.routing import BaseRoute


def IterateKonomiTVBS4KAPIRouteContexts(routes: Sequence[BaseRoute]) -> Iterator[RouteContext]:
    """FastAPI の router tree に含まれる有効な APIRoute を平坦に列挙する。

    Args:
        routes: 列挙対象の FastAPI / Starlette ルート。

    Returns:
        include_router() の prefix や dependencies を反映した APIRoute の文脈。
    """

    # FastAPI 0.137 以降は include_router() が中間ノードを保持するため、
    # フレームワーク側の traversal を使って各 APIRoute の有効な文脈まで展開する。
    for route_context in iter_route_contexts(routes):
        if isinstance(route_context.original_route, APIRoute):
            yield route_context
