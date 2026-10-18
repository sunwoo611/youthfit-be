"""일 배치 적재 CLI: `uv run python -m app.ingest --source all|ontong|fss`."""

import argparse
import asyncio
import logging
import sys

from app.core.config import get_settings
from app.core.database import dispose_engine, get_engine
from app.ingest.fss import FssClient
from app.ingest.http import ApiHttpClient
from app.ingest.ontong import OntongClient
from app.ingest.pipeline import SourceReport, format_report, run_fss, run_ontong

SOURCES = ("all", "ontong", "fss")


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

    reports: list[SourceReport] = []
    try:
        async with ApiHttpClient() as http:
            try:
                ontong = OntongClient.from_settings(settings, http) if source != "fss" else None
                fss = FssClient.from_settings(settings, http) if source != "ontong" else None
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2

            if ontong:
                reports.append(await run_ontong(engine, ontong))
            if fss:
                reports.append(await run_fss(engine, fss))
            api_calls = http.request_count
    finally:
        await dispose_engine()

    for report in reports:
        print(format_report(report))
    print(f"API 호출 {api_calls}회")
    return 1 if any(r.status == "failed" for r in reports) else 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sys.exit(asyncio.run(main(parse_args().source)))
