"""오래된 역질문 세션 삭제 CLI: `uv run python -m app.jobs.cleanup_sessions`.

일 배치 워크플로에서 적재와 별도 단계로 돈다. 보관 기간은 `SESSION_RETENTION_DAYS`.
"""

import asyncio
import sys
from datetime import timedelta

from app.core.config import get_settings
from app.core.database import dispose_engine, get_engine
from app.services.session_service import delete_old_sessions


async def main() -> int:
    settings = get_settings()
    engine = get_engine(settings)
    if engine is None:
        print(".env 에 DATABASE_URL 을 설정하세요", file=sys.stderr)
        return 2
    days = settings.session_retention_days
    try:
        async with engine.begin() as conn:
            deleted = await delete_old_sessions(conn, timedelta(days=days))
    finally:
        await dispose_engine()
    print(f"세션 {deleted}건 삭제 (보관 {days}일)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
