"""Isolated failure scenarios: real CLI/use case/SQLite, synthetic network gateways."""
import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from config.settings import get_settings
from src.adapters.catalog.mysql_reader import MySQLConnectionError
from src.adapters.crawler.web_crawler import CrawlResult
from src.adapters.db.sqlite_repo import SQLiteProductRepository
from src.domain.entities import AuditResult, AuditStatus, DiscrepancyItem, Product
from src.entrypoints import cli
from src.use_cases.resolve_reference import ResolvedReference


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def product(pid: int, title: str | None = None) -> Product:
    return Product(
        product_id=pid, title=title or f"Test device model {pid}",
        current_specs={"Voltage": "5 V"}, images_count=3, images_count_known=True,
    )


class RunHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.repo = SQLiteProductRepository(tmp_path / "isolated.db")
        self.batches: list[list[Product]] = []
        self.events: list[str] = []
        self.filters: dict[str, object] = {}
        self.connect_error: Exception | None = None
        self.read_error: Exception | None = None
        self.error_products: set[int] = set()
        self.not_found_products: set[int] = set()
        self.judge_errors: set[int] = set()
        self.cancel_product: int | None = None
        self.concurrent = False
        self.reader_closed = False
        self.crawler_closed = False
        self.judge_closed = False
        owner = self

        class Reader:
            async def __aenter__(self):
                owner.events.append("connect")
                if owner.connect_error:
                    raise owner.connect_error
                return self

            async def __aexit__(self, *args):
                owner.reader_closed = True

            async def stream_products(self, **kwargs) -> AsyncGenerator[list[Product]]:
                owner.filters = kwargs
                for index, batch in enumerate(owner.batches):
                    owner.events.append(f"read:{index}")
                    yield batch
                if owner.read_error:
                    raise owner.read_error

        class Crawler:
            def __init__(self, **kwargs):
                self.ready = False

            async def start(self):
                self.ready = True

            async def close(self):
                owner.crawler_closed = True

            async def crawl(self, url):
                assert self.ready
                return CrawlResult(url=url, success=True, markdown="Voltage: 5 V", title="Reference", images_count=3)

        class Resolver:
            def __init__(self, **kwargs):
                self.configured = True

            async def execute(self, item, **kwargs):
                owner.events.append(f"audit:{item.product_id}")
                if item.product_id == owner.cancel_product:
                    raise asyncio.CancelledError
                if owner.concurrent:
                    await asyncio.sleep(0.01)
                if item.product_id in owner.error_products:
                    raise httpx.ConnectError("Изолированная ошибка загрузки эталона")
                urls = [] if item.product_id in owner.not_found_products else [f"https://example.com/{item.product_id}"]
                return ResolvedReference(
                    product_id=item.product_id, status="FOUND" if urls else "NOT_FOUND",
                    scenario_applied="SCENARIO_3", reference_url=urls[0] if urls else None,
                    candidate_urls=urls, source_type="MODEL_SEARCH", query_used=item.title,
                )

        class Judge:
            def __init__(self, **kwargs):
                self.active = True

            async def judge(self, product, reference_url, **kwargs):
                return AuditResult(
                    product_id=product.product_id,
                    status=AuditStatus.ERROR if product.product_id in owner.judge_errors else AuditStatus.MISMATCH,
                    reference_url=reference_url, details="Synthetic model answer", matched_specs_count=1,
                )

            async def aclose(self):
                owner.judge_closed = True

        settings = get_settings().model_copy(update={"OPENAI_API_KEY": "synthetic-test-key"})
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        monkeypatch.setattr(cli, "setup_utf8_terminal", lambda: None)
        monkeypatch.setattr(cli, "SQLiteProductRepository", lambda path: self.repo)
        monkeypatch.setattr(cli, "MySQLCatalogReader", Reader)
        monkeypatch.setattr(cli, "WebCrawler", Crawler)
        monkeypatch.setattr(cli, "OpenAIJudge", Judge)
        monkeypatch.setattr(cli, "ResolveReferenceUseCase", Resolver)

    def args(self, *args: str):
        return cli.build_parser().parse_args(["audit", "--db", str(self.repo.db_path), *args])


@pytest.fixture
def run_harness(monkeypatch, tmp_path):
    return RunHarness(monkeypatch, tmp_path)


@pytest.mark.anyio
async def test_cli_filtered_batches_skip_and_limit(run_harness, capsys):
    h = run_harness
    first = product(1)
    h.repo.save_product_and_audit_result(first, AuditResult(
        product_id=1, status=AuditStatus.VERIFIED, reference_url="https://example.com/1",
        audit_parts={"specs": first.audit_input_hash("specs"), "photos": first.audit_input_hash("photos", "https://example.com/1")},
    ))
    h.batches = [[first], [product(2)], [product(3)], [product(4)]]
    code = await cli.run_audit_command(h.args("--group", "78", "--main-cat", "11", "--limit", "2", "--offset", "7"))
    output = capsys.readouterr().out
    assert code == 0
    assert h.events == ["connect", "read:0", "read:1", "audit:2", "read:2", "audit:3"]
    assert h.filters["category_id"] == 78 and h.filters["main_category_id"] == 11
    assert h.filters["offset"] == 7 and h.filters["batch_size"] == 100
    assert "Обработано: 2 | Пропущено: 1 | Не удалось проверить: 0 | С замечаниями: 2" in output
    assert "достигнут лимит 2" in output
    assert "[0002] Product ID #3" in output
    assert h.repo.get_product_by_id(4) is None
    assert h.reader_closed and h.crawler_closed and h.judge_closed


@pytest.mark.anyio
async def test_cli_zero_limit_exhausts_selection_and_deduplicates_across_batches(run_harness, capsys):
    h = run_harness
    h.batches = [[product(1)], [product(2, "Test device model 1")], [product(3)]]
    assert await cli.run_audit_command(h.args("--group", "78", "--limit", "0")) == 0
    output = capsys.readouterr().out
    assert h.events == ["connect", "read:0", "audit:1", "read:1", "read:2", "audit:3"]
    assert "выборка закончилась" in output
    assert "Обработано: 2 | Пропущено: 1" in output


@pytest.mark.anyio
@pytest.mark.parametrize("all_skipped", [False, True])
async def test_cli_successful_empty_or_all_skipped_is_not_mysql_error(run_harness, capsys, all_skipped):
    h = run_harness
    if all_skipped:
        item = product(1)
        h.repo.save_product_and_audit_result(item, AuditResult(
            product_id=1, status=AuditStatus.VERIFIED, reference_url="https://example.com/1",
            audit_parts={"specs": item.audit_input_hash("specs"), "photos": item.audit_input_hash("photos", "https://example.com/1")},
        ))
        h.batches = [[item]]
    assert await cli.run_audit_command(h.args("--group", "78", "--limit", "0")) == 0
    output = capsys.readouterr().out
    assert "Новых товаров для проверки нет" in output and "запуск завершён" in output
    assert "MySQL connection" not in output and "local DB" not in output
    assert h.reader_closed and not h.crawler_closed


@pytest.mark.anyio
async def test_cli_product_failure_preserves_previous_report_and_continues(run_harness, capsys):
    h = run_harness
    first = product(1)
    old = AuditResult(product_id=1, status=AuditStatus.MISMATCH, details="Valuable earlier findings")
    h.repo.save_product_and_audit_result(first, old)
    h.error_products = {1}
    h.batches = [[first, product(2)], [product(3)]]
    assert await cli.run_audit_command(h.args("--limit", "0", "--no-skip-audited")) == 1
    output = capsys.readouterr().out
    assert "Изолированная ошибка" in output and "эталона" in output
    assert "Продолжаю проверку остальных товаров" in output
    assert "Обработано: 3 | Пропущено: 0 | Не удалось проверить: 1" in output
    assert h.repo.get_latest_audit_result(1).details == "Valuable earlier findings"
    assert h.repo.get_latest_audit_result(3).status == AuditStatus.MISMATCH


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["not_found_products", "judge_errors"])
async def test_cli_not_found_and_model_failure_are_unsuccessful_checks(run_harness, capsys, mode):
    h = run_harness
    setattr(h, mode, {1})
    h.batches = [[product(1), product(2)]]
    assert await cli.run_audit_command(h.args()) == 1
    assert "Обработано: 2 | Пропущено: 0 | Не удалось проверить: 1" in capsys.readouterr().out
    assert h.repo.get_latest_audit_result(2) is not None


@pytest.mark.anyio
async def test_cli_catalog_loss_keeps_committed_batch_and_stops(run_harness, capsys):
    h = run_harness
    h.batches = [[product(1)]]
    h.read_error = MySQLConnectionError("Изолированная потеря соединения при чтении следующей порции")
    assert await cli.run_audit_command(h.args("--group", "78", "--limit", "0")) == 2
    output = capsys.readouterr().out
    assert "запуск прерван" in output and "Выборка не закончена" in output
    assert "Обработано: 1" in output and "Falling back" not in output
    assert h.repo.get_latest_audit_result(1).status == AuditStatus.MISMATCH
    assert h.reader_closed and h.crawler_closed and h.judge_closed


@pytest.mark.anyio
async def test_cli_storage_failure_rolls_back_current_product_and_stops(run_harness, capsys):
    h = run_harness
    with sqlite3.connect(h.repo.db_path) as connection:
        connection.execute("CREATE TRIGGER fail_second BEFORE INSERT ON audit_results WHEN NEW.product_id=2 BEGIN SELECT RAISE(ABORT, 'isolated disk failure'); END")
    h.batches = [[product(1), product(2), product(3)], [product(4)]]
    assert await cli.run_audit_command(h.args("--limit", "0")) == 2
    output = capsys.readouterr().out
    assert "isolated disk failure" in output
    assert h.events == ["connect", "read:0", "audit:1", "audit:2"]
    assert h.repo.get_latest_audit_result(1) is not None
    assert h.repo.get_product_by_id(2) is None
    assert h.repo.get_latest_audit_result(2) is None
    assert h.repo.get_latest_audit_result(3) is None
    assert h.reader_closed and h.crawler_closed and h.judge_closed


@pytest.mark.anyio
async def test_cli_connection_error_does_not_fallback(run_harness, capsys):
    h = run_harness
    h.connect_error = MySQLConnectionError("Изолированная ошибка соединения")
    assert await cli.run_audit_command(h.args("--group", "78")) == 2
    output = capsys.readouterr().out
    assert "Изолированная ошибка соединения" in output
    assert "local DB" not in output and "запуск завершён" not in output
    assert h.events == ["connect"]


@pytest.mark.anyio
@pytest.mark.parametrize("options", [
    ["--group", "78", "--product-id", "1"], ["--main-cat", "1", "--sku", "123"],
    ["--group", "78", "--source", "crawler"], ["--limit", "-1"], ["--offset", "-1"],
])
async def test_cli_invalid_selection_fails_before_network(run_harness, capsys, options):
    h = run_harness
    assert await cli.run_audit_command(h.args(*options)) == 2
    assert h.events == []
    assert "запуск прерван" in capsys.readouterr().out


@pytest.mark.anyio
async def test_cli_interruption_closes_resources_and_returns_130(run_harness, capsys):
    h = run_harness
    h.cancel_product = 2
    h.batches = [[product(1), product(2), product(3)]]
    assert await cli.run_audit_command(h.args("--limit", "0")) == 130
    assert "остановлен пользователем" in capsys.readouterr().out
    assert h.repo.get_latest_audit_result(1) is not None
    assert h.repo.get_product_by_id(3) is None
    assert h.reader_closed and h.crawler_closed and h.judge_closed


@pytest.mark.anyio
async def test_cli_explicit_lookup_is_preserved(run_harness, monkeypatch, capsys):
    h = run_harness
    sync = AsyncMock(return_value=[product(7)])
    monkeypatch.setattr(cli, "_live_sync_products", sync)
    assert await cli.run_audit_command(h.args("--sku", "sku7")) == 0
    sync.assert_awaited_once_with(["sku7"], h.repo, source="mysql")
    assert "audit:7" in h.events
    assert h.repo.get_product_by_id(7).master_key == "test_device_model_7"
    assert "Product ID #7" in capsys.readouterr().out


@pytest.mark.anyio
async def test_cli_fatal_error_settles_inflight_without_starting_more(run_harness, capsys):
    h = run_harness
    h.concurrent = True
    with sqlite3.connect(h.repo.db_path) as connection:
        connection.execute("CREATE TRIGGER fail_first BEFORE INSERT ON audit_results WHEN NEW.product_id=1 BEGIN SELECT RAISE(ABORT, 'isolated storage fault'); END")
    h.batches = [[product(1), product(2), product(3), product(4)], [product(5)]]
    assert await cli.run_audit_command(h.args("--limit", "0", "--concurrency", "2")) == 2
    assert h.events == ["connect", "read:0", "audit:1", "audit:2"]
    assert h.repo.get_product_by_id(1) is None
    assert h.repo.get_latest_audit_result(2) is not None
    assert h.repo.get_product_by_id(3) is None
    assert "Обработано: 2" in capsys.readouterr().out
    assert h.reader_closed and h.crawler_closed and h.judge_closed


@pytest.mark.anyio
async def test_cli_explicit_products_bypass_mass_limit_and_incremental_skip(run_harness, monkeypatch):
    h = run_harness
    items = [product(pid) for pid in range(1, 7)]
    for item in items:
        h.repo.save_product_and_audit_result(item, AuditResult(
            product_id=item.product_id, status=AuditStatus.VERIFIED, reference_url="https://example.com/item",
            audit_parts={"specs": item.audit_input_hash("specs"), "photos": item.audit_input_hash("photos", "https://example.com/item")},
        ))
    monkeypatch.setattr(cli, "_live_sync_products", AsyncMock(return_value=items))
    assert await cli.run_audit_command(h.args("--product-id", *[str(pid) for pid in range(1, 7)])) == 0
    assert [event for event in h.events if event.startswith("audit:")] == [f"audit:{pid}" for pid in range(1, 7)]


@pytest.mark.anyio
async def test_cli_missing_model_key_stops_before_auditing_selection(run_harness, monkeypatch, capsys):
    h = run_harness
    settings = get_settings().model_copy(update={"OPENAI_API_KEY": None})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    h.batches = [[product(1)], [product(2)]]
    assert await cli.run_audit_command(h.args("--limit", "0")) == 2
    assert h.events == ["connect", "read:0"]
    assert "OPENAI_API_KEY" in capsys.readouterr().out


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["error_products", "not_found_products", "judge_errors", "crawl_failure"])
@pytest.mark.parametrize("skip_description", [False, True])
async def test_cli_failed_full_recheck_keeps_findings_but_next_batch_retries(
    run_harness, monkeypatch, capsys, failure, skip_description,
):
    h = run_harness
    item = product(1).model_copy(update={"description": "Original description"})
    url = "https://example.com/original-proof"
    old = AuditResult(
        product_id=1, status=AuditStatus.MISMATCH, reference_url=url,
        details="Valuable earlier findings",
        discrepancies=[DiscrepancyItem(spec_name="Voltage", shop_value="5 V", reference_value="12 V", severity="critical", proof_quote="Voltage: 12 V", source_url=url)],
        audit_parts={"specs": item.audit_input_hash("specs"), "description": item.audit_input_hash("description"),
                     "photos": item.audit_input_hash("photos", url), "photos_url": url},
    )
    h.repo.save_product_and_audit_result(item, old)
    if failure == "crawl_failure":
        original_crawler = cli.WebCrawler
        class FailingCrawler(original_crawler):
            async def crawl(self, url):
                return CrawlResult(url=url, success=False, markdown="", error="isolated crawl failure")
        monkeypatch.setattr(cli, "WebCrawler", FailingCrawler)
    else:
        setattr(h, failure, {1})
    monkeypatch.setattr(cli, "_live_sync_products", AsyncMock(return_value=[item]))
    options = ["--product-id", "1", "--force-refresh"]
    if skip_description:
        options.append("--skip-desc")
    assert await cli.run_audit_command(h.args(*options)) == 1
    failed_output = capsys.readouterr().out
    assert "Не удалось проверить: 1" in failed_output
    retained = h.repo.get_latest_audit_result(1)
    assert retained.status == old.status and retained.details == old.details
    assert retained.reference_url == old.reference_url and retained.discrepancies == old.discrepancies
    assert retained.audit_parts.get("photos_url") == url
    assert "specs" not in retained.audit_parts and "photos" not in retained.audit_parts
    assert (retained.audit_parts.get("description") == old.audit_parts["description"]) == skip_description
    h.events.clear()
    h.batches = [[item]]
    # Keep the isolated failure active: the next normal incremental run must attempt it again.
    assert await cli.run_audit_command(h.args("--limit", "0")) == 1
    retry_output = capsys.readouterr().out
    assert "audit:1" in h.events
    assert "Обработано: 1 | Пропущено: 0 | Не удалось проверить: 1" in retry_output
    assert h.repo.get_latest_audit_result(1).discrepancies == old.discrepancies
