"""앱 로거 설정.

uvicorn 기본 설정은 `uvicorn` 로거만 잡으므로, 따로 설정하지 않으면 `app.*` 의 INFO 로그
(LLM 토큰 사용량 등)가 버려진다.
"""

import logging

APP_LOGGER = "app"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """`app` 로거를 level 로 열고, 출력할 핸들러가 어디에도 없을 때만 stderr 핸들러를 붙인다.

    `--log-config` 등으로 이미 설정했으면 그 설정을 따른다. 전파는 켜 둔다(pytest caplog 는
    루트에서 받는다).
    """
    logger = logging.getLogger(APP_LOGGER)
    if logger.level == logging.NOTSET:
        logger.setLevel(level)
    if logger.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
