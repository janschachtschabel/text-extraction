"""Fixtures of the browser tests: Chromium, the local test site and a browser stack per test.

The tests run a real Chromium (``@pytest.mark.browser``; the default ``pytest`` run leaves them
out). Chromium is ``BROWSER_EXECUTABLE_PATH`` when set, else Playwright's own, else a headless
shell under ``/opt/pw-browsers``; without one the tests are skipped.

The site is served on three loopback ports. The egress proxy of the tests admits 127.0.0.1 on
the first two only (the site and its "third party"), so the private-address tests still see the
third port and ``169.254.169.254`` refused.
"""

import asyncio
import glob
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from fakeredis import FakeAsyncRedis
from playwright.async_api import Browser, BrowserContext, async_playwright
from prometheus_client import CollectorRegistry

from tests.browser.site import Site
from tests.fakes import FakeArtifacts, FakeFetcher
from text_extraction.auth import Principal
from text_extraction.browser import (
    BrowserPool,
    ChromiumRenderer,
    PageProxy,
    PlaywrightLauncher,
)
from text_extraction.browser_proxy import EgressProxy, _Refusal
from text_extraction.compliance import ComplianceChecker
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher
from text_extraction.fingerprint import FingerprintStore
from text_extraction.outbound import DomainGate
from text_extraction.pipeline import Pipeline
from text_extraction.render import ArtifactSink, RenderOptions
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
_SHELL_GLOB = (
    "/opt/pw-browsers/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
)


async def _playwright_own_chromium_works() -> bool:
    try:
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            await browser.close()
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def chromium() -> Path | None:
    """The Chromium to launch: a path, or None for Playwright's own; skips without one."""
    configured = os.environ.get("BROWSER_EXECUTABLE_PATH")
    if configured:
        return Path(configured)
    if asyncio.run(_playwright_own_chromium_works()):
        return None
    found = sorted(glob.glob(_SHELL_GLOB))
    if found:
        return Path(found[-1])
    pytest.skip("no Chromium found: set BROWSER_EXECUTABLE_PATH or run `playwright install`")


@pytest.fixture(scope="session")
def site() -> Iterator[Site]:
    served = Site()
    served.serve()
    yield served
    served.close()


@pytest.fixture(autouse=True)
def _clear_site_log(site: Site) -> None:
    site.clear()


class SiteProxy(EgressProxy):
    """The egress proxy with the test policy: 127.0.0.1, and only on the site's two ports."""

    def __init__(self, settings: Settings, ports: set[int], registry: CollectorRegistry) -> None:
        super().__init__(settings, policy=_loopback_only, registry=registry)
        self._ports = ports

    async def _dial(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if port not in self._ports:
            raise _Refusal(403, "blocked_address", host)
        return await super()._dial(host, port)


def _loopback_only(address: IPv4Address | IPv6Address) -> bool:
    return address == IPv4Address("127.0.0.1")


class RecordingLauncher(PlaywrightLauncher):
    """The Playwright launcher, noting each browser it launches."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.browsers: list[Browser] = []
        self.launch_delay = 0.0  # seconds before each launch

    async def open(
        self, browser: Browser | None, proxy: PageProxy, options: RenderOptions
    ) -> tuple[Browser | None, BrowserContext]:
        if browser is None and self.launch_delay:
            await asyncio.sleep(self.launch_delay)
        opened, context = await super().open(browser, proxy, options)
        if opened is not None and opened is not browser:
            self.browsers.append(opened)
        return opened, context


@dataclass
class Stack:
    settings: Settings
    registry: CollectorRegistry
    proxy: SiteProxy
    launcher: RecordingLauncher
    pool: BrowserPool
    renderer: ChromiumRenderer

    def metric(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0


@pytest.fixture
def settings_for(chromium: Path | None) -> Any:
    def make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "auth_mode": "none",
            "outbound_min_delay_ms": 0,
            "browser_enabled": True,
            # R9: the pipeline's navigation check refuses private IP literals; the test proxy
            # still refuses everything but the site's two ports.
            "fetch_allow_private_networks": True,
            "browser_executable_path": chromium,
            "browser_idle_wait_seconds": 1,
        }
        values.update(overrides)
        return Settings(**values)

    return make


@asynccontextmanager
async def open_stack(settings: Settings, site: Site) -> AsyncIterator[Stack]:
    """A proxy, a pool with a launched browser and the renderer, all stopped afterwards."""
    registry = CollectorRegistry()
    ports = {urlsplit(site.base).port or 0, urlsplit(site.third).port or 0}
    proxy = SiteProxy(settings, ports, registry)
    await proxy.start()
    launcher = RecordingLauncher(settings)
    pool = BrowserPool(settings, proxy, registry, launcher=launcher)
    try:
        await pool.start()
        yield Stack(settings, registry, proxy, launcher, pool, ChromiumRenderer(pool, settings))
    finally:
        await pool.stop()
        await proxy.stop()


def pipeline_for(stack: Stack) -> Pipeline:
    """The real pipeline with the stack's renderer. Compliance files come from a fake fetcher
    (all 404: allowed), so no fast request leaves for a forbidden address."""
    valkey = FakeAsyncRedis()
    fetcher = cast(Fetcher, FakeFetcher({}))
    return Pipeline(
        fetcher,
        DomainGate(valkey, stack.settings),
        ComplianceChecker(fetcher, valkey, stack.settings),
        ResultCache(valkey, stack.settings),
        FingerprintStore(valkey, stack.settings),
        stack.settings,
        stack.registry,
        renderer=stack.renderer,
        timeout_seconds=stack.settings.browser_timeout_seconds,
    )


async def run_pipeline(
    stack: Stack, *, artifacts: FakeArtifacts | None = None, **request: Any
) -> Envelope[ExtractResult]:
    request.setdefault("mode", "browser")
    return await pipeline_for(stack).run(
        ExtractRequest(**request), USER, artifacts=cast(ArtifactSink | None, artifacts)
    )
