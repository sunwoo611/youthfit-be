import pytest

from app.ingest import __main__ as cli
from app.ingest.pipeline import SourceReport


@pytest.mark.parametrize("source", ["all", "ontong"])
def test_parse_args_accepts_legacy_sources(source: str):
    assert cli.parse_args(["--source", source]).source == source


def test_parse_args_rejects_removed_fss_source():
    with pytest.raises(SystemExit):
        cli.parse_args(["--source", "fss"])


class FakeOntongClient:
    @classmethod
    def from_settings(cls, settings, http) -> "FakeOntongClient":
        return cls()


@pytest.mark.parametrize(("status", "code"), [("success", 0), ("partial", 0), ("failed", 1)])
async def test_main_exit_code_follows_report_status(
    monkeypatch: pytest.MonkeyPatch, status: str, code: int
):
    async def run_ontong(engine, client) -> SourceReport:
        return SourceReport(source="ontong", status=status)

    async def dispose_engine() -> None:
        return None

    monkeypatch.setattr(cli, "get_engine", lambda settings: object())
    monkeypatch.setattr(cli, "dispose_engine", dispose_engine)
    monkeypatch.setattr(cli, "OntongClient", FakeOntongClient)
    monkeypatch.setattr(cli, "run_ontong", run_ontong)

    assert await cli.main() == code


async def test_main_without_database_url_returns_2(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "get_engine", lambda settings: None)
    assert await cli.main() == 2
