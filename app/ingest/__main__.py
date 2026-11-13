"""일 배치 적재 CLI: `uv run python -m app.ingest --source all|ontong`.

적재 소스는 온통청년 하나다. `all` 은 기존 호출(워크플로·문서)과의 호환을 위해 남긴다.
"""

import argparse
import asyncio
import logging
import sys

from app.core.config import get_settings
from app.core.database import dispose_engine, get_engine
from app.ingest.http import ApiHttpClient
from app.ingest.ontong import OntongClient
from app.ingest.pipeline import format_report, run_ontong

SOURCES = ("all", "ontong")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.ingest", description=__doc__)
    parser.add_argument("--source", choices=SOURCES, default="all")
    return parser.parse_args(argv)


async def main(source: str) -> int:
    settings = get_settings()
    engine = get_engine(settings)
    if engine is None:
        print(".env 에 DATABASE_URL 을 설정하세요", file=sys.stderr)
        return 2

    try:
        async with ApiHttpClient() as http:
            try:
                ontong = OntongClient.from_settings(settings, http)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2
            report = await run_ontong(engine, ontong)
            api_calls = http.request_count
    finally:
        await dispose_engine()

    print(format_report(report))
    print(f"API 호출 {api_calls}회")
    return 1 if report.status == "failed" else 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sys.exit(asyncio.run(main(parse_args().source)))
